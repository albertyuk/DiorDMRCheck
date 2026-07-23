"""SQLite persistence: link cache, runs, human overrides.

Connections are opened per call (WAL mode) so the background run thread and
request handlers never share a connection.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from typing import Any, Optional

from .. import config
from . import migrations

_init_lock = threading.Lock()
# Schema is applied once per database path, so tests that repoint
# config.DB_PATH get a fresh schema automatically — no flag to reset.
_initialized_paths: set[str] = set()
logger = logging.getLogger(__name__)


class StorageLimitError(RuntimeError):
    """A durable result would exceed a configured storage safety bound."""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    path = str(config.DB_PATH)
    if path not in _initialized_paths:
        with _init_lock:
            if path not in _initialized_paths:
                migrations.apply(conn)
                _initialized_paths.add(path)
    return conn


# ---------------------------------------------------------------- link cache

_CACHE_COLUMNS = (
    "status", "note_id", "author_id", "author_name", "likes", "collects",
    "comments", "title", "publish_time", "source", "error", "raw_json",
    "resolved_at", "author_failed_at",
)

def cache_get(url: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM link_cache WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def cache_put(url: str, **fields: Any) -> None:
    unknown = set(fields) - set(_CACHE_COLUMNS)
    if unknown:
        raise ValueError(f"cache_put: unknown field(s) {sorted(unknown)}")
    if fields.get("status") not in {"ok", "failed"}:
        raise ValueError("cache_put requires status='ok' or status='failed'")
    fields.setdefault("resolved_at", time.time())
    values = [fields.get(c) for c in _CACHE_COLUMNS]
    # Once a URL has a successful immutable resolution, a racing transient
    # failure (or a less complete success) must not erase it. Failed rows are
    # freely replaced by fresher attempts. Author enrichment is performed by
    # cache_merge and may still fill/clear individual fields atomically.
    updates = [
        "status=CASE WHEN link_cache.status='ok' THEN 'ok' "
        "ELSE excluded.status END"
    ]
    for column in _CACHE_COLUMNS:
        if column == "status":
            continue
        if column == "error":
            expression = (
                "CASE WHEN link_cache.status='ok' THEN link_cache.error "
                "WHEN excluded.status='ok' THEN NULL "
                "ELSE COALESCE(excluded.error, link_cache.error) END"
            )
        elif column == "resolved_at":
            expression = (
                "CASE WHEN link_cache.status='ok' THEN link_cache.resolved_at "
                "ELSE excluded.resolved_at END"
            )
        else:
            expression = (
                f"CASE WHEN link_cache.status='ok' "
                f"THEN COALESCE(link_cache.{column}, excluded.{column}) "
                f"ELSE COALESCE(excluded.{column}, link_cache.{column}) END"
            )
        updates.append(f"{column}={expression}")
    with connect() as conn:
        conn.execute(
            f"INSERT INTO link_cache (url, {', '.join(_CACHE_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in _CACHE_COLUMNS)}) "
            "ON CONFLICT(url) DO UPDATE SET "
            + ", ".join(updates),
            [url, *values],
        )
        conn.commit()


def cache_merge(url: str, **fields: Any) -> None:
    """Update the provided fields (verbatim — an explicit None clears the
    field) on an existing cache row, leaving all other fields untouched."""
    if not fields:
        return
    unknown = set(fields) - set(_CACHE_COLUMNS)
    if unknown:
        raise ValueError(f"cache_merge: unknown field(s) {sorted(unknown)}")
    if "status" in fields and fields["status"] not in {"ok", "failed"}:
        raise ValueError("cache status must be 'ok' or 'failed'")
    sets: list[str] = []
    values: list[Any] = []
    for column, value in fields.items():
        if column == "status":
            sets.append(
                "status = CASE WHEN status='ok' THEN 'ok' ELSE ? END"
            )
        else:
            sets.append(f"{column} = ?")
        values.append(value)
    with connect() as conn:
        changed = conn.execute(
            f"UPDATE link_cache SET {', '.join(sets)} WHERE url = ?",
            [*values, url],
        ).rowcount
        if changed != 1:
            raise KeyError(f"cannot enrich uncached URL {url!r}")
        conn.commit()


def cache_prune(max_rows: int, max_raw_bytes: int) -> dict[str, int]:
    """Bound cache rows and optional raw payloads, oldest-first.

    Raw API bodies are discarded before resolved identities because matching
    needs the extracted fields, not the original response. If the row bound is
    exceeded, failures are evicted before successful resolutions.
    """
    removed_rows = 0
    stripped_payloads = 0
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        raw_total = conn.execute(
            "SELECT COALESCE(SUM(length(CAST(raw_json AS BLOB))), 0) "
            "FROM link_cache"
        ).fetchone()[0]
        while raw_total > max_raw_bytes:
            rows = conn.execute(
                "SELECT url, length(CAST(raw_json AS BLOB)) AS n "
                "FROM link_cache WHERE raw_json IS NOT NULL "
                "ORDER BY resolved_at LIMIT 500"
            ).fetchall()
            if not rows:
                break
            conn.executemany(
                "UPDATE link_cache SET raw_json = NULL WHERE url = ?",
                [(row["url"],) for row in rows],
            )
            raw_total -= sum(row["n"] or 0 for row in rows)
            stripped_payloads += len(rows)

        count = conn.execute("SELECT COUNT(*) FROM link_cache").fetchone()[0]
        while count > max_rows:
            n = min(500, count - max_rows)
            urls = conn.execute(
                "SELECT url FROM link_cache "
                "ORDER BY CASE status WHEN 'failed' THEN 0 ELSE 1 END, "
                "resolved_at LIMIT ?", (n,)
            ).fetchall()
            if not urls:
                break
            conn.executemany(
                "DELETE FROM link_cache WHERE url = ?",
                [(row["url"],) for row in urls],
            )
            removed_rows += len(urls)
            count -= len(urls)
        conn.commit()
    return {"rows": removed_rows, "payloads": stripped_payloads}


# ---------------------------------------------------------------------- runs

def run_create(run_id: str, **fields: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, created_at, status, plog_path, dmr_path, "
            "plog_name, dmr_name, options_json, preview_json, perimeter_hash, "
            "perimeter_uploaded, perimeter_name) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, time.time(),
                fields.get("plog_path"), fields.get("dmr_path"),
                fields.get("plog_name"), fields.get("dmr_name"),
                # NULL until the user actually starts the run — the run page
                # uses this to distinguish "not started" from "starting".
                json.dumps(fields["options"]) if fields.get("options") is not None else None,
                json.dumps(fields.get("preview") or {}, ensure_ascii=False, default=str),
                fields.get("perimeter_hash"),
                int(bool(fields.get("perimeter_uploaded"))),
                fields.get("perimeter_name"),
            ),
        )
        conn.commit()


# The mutable columns of the runs table. run_update interpolates column
# names into SQL, so anything outside this set must raise — a future caller
# passing user-influenced keys must never become SQL injection.
_RUN_UPDATE_COLUMNS = frozenset({
    "status", "phase", "progress_done", "progress_total", "message",
    "options_json", "preview_json", "result_json", "summary_json",
    "tikhub_calls", "llm_calls", "error", "perimeter_hash",
})


def run_update(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    unknown = set(fields) - _RUN_UPDATE_COLUMNS
    if unknown:
        raise ValueError(f"run_update: unknown column(s) {sorted(unknown)}")
    result_json = fields.get("result_json")
    result_bytes = (len(result_json.encode("utf-8"))
                    if isinstance(result_json, str) else 0)
    if result_bytes > config.MAX_RESULT_BYTES:
        raise StorageLimitError(
            f"Serialized run result exceeds the {config.MAX_RESULT_MB} MB limit."
        )
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        if result_json is not None:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT COALESCE(length(CAST(result_json AS BLOB)), 0) "
                "FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            projected = (_logical_storage_bytes(conn)
                         - (previous[0] if previous else 0) + result_bytes)
            if projected > config.DB_MAX_TOTAL_BYTES:
                conn.rollback()
                raise StorageLimitError(
                    "Database content would exceed the configured storage limit."
                )
        conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", [*fields.values(), run_id])
        conn.commit()


def run_get(run_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def run_list(limit: int = 30) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, phase, plog_name, dmr_name, message "
            "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def run_delete(run_id: str) -> None:
    """Delete an expired run and its dependent human overrides."""
    with connect() as conn:
        conn.execute("DELETE FROM overrides WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()


def run_progress(run_id: str, phase: str, done: int, total: int, message: str) -> None:
    run_update(run_id, phase=phase, progress_done=done, progress_total=total,
               message=message)


def run_bump_counter(run_id: str, column: str, amount: int = 1) -> None:
    if column not in ("tikhub_calls", "llm_calls"):
        # a raise, not an assert — asserts vanish under `python -O`
        raise ValueError(f"run_bump_counter: unknown counter {column!r}")
    with connect() as conn:
        conn.execute(
            f"UPDATE runs SET {column} = COALESCE({column}, 0) + ? WHERE id = ?",
            (amount, run_id),
        )
        conn.commit()


# ----------------------------------------------------------------- overrides

def override_set(run_id: str, excel_row: int, campaign: str, no: str,
                 status: str, note: str = "", updated_by: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO overrides (run_id, excel_row, campaign, no, status, note, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, excel_row) DO UPDATE SET "
            "campaign=excluded.campaign, no=excluded.no, "
            "status=excluded.status, note=excluded.note, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (run_id, excel_row, campaign, no, status, note, updated_by, time.time()),
        )
        conn.commit()


# --------------------------------------------------------------------- users

def user_get(username: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchone()
    return dict(row) if row else None


def user_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def user_list() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT username, display, is_admin, created_at FROM users "
            "ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def user_upsert(username: str, password_hash: str, display: str = "",
                is_admin: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, display, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET display=excluded.display, "
            "password_hash=excluded.password_hash, is_admin=excluded.is_admin",
            (username, display, password_hash, int(is_admin), time.time()),
        )
        conn.commit()


def user_create(username: str, password_hash: str, display: str = "",
                is_admin: bool = False) -> bool:
    """Create a user without allowing a concurrent request to reset one."""
    with connect() as conn:
        changed = conn.execute(
            "INSERT OR IGNORE INTO users "
            "(username, display, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, display, password_hash, int(is_admin), time.time()),
        ).rowcount
        conn.commit()
    return changed == 1


def user_set_password(username: str, password_hash: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                     (password_hash, username))
        conn.commit()


def user_delete(username: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()


def user_delete_guarded(actor: str, username: str) -> str:
    """Atomically enforce self/last-admin invariants and delete a user."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor_row = conn.execute(
            "SELECT is_admin FROM users WHERE username = ?", (actor,)
        ).fetchone()
        if actor_row is None or not actor_row["is_admin"]:
            conn.rollback()
            return "forbidden"
        target = conn.execute(
            "SELECT is_admin FROM users WHERE username = ?", (username,)
        ).fetchone()
        if target is None:
            conn.rollback()
            return "missing"
        if username == actor:
            conn.rollback()
            return "self"
        if target["is_admin"]:
            admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1"
            ).fetchone()[0]
            if admins <= 1:
                conn.rollback()
                return "last_admin"
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return "deleted"


def admin_count() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


# ---------------------------------------------------- perimeter cache + kv

def perimeter_cache_get(file_hash: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM perimeter_cache WHERE file_hash = ?",
                           (file_hash,)).fetchone()
    return dict(row) if row else None


def perimeter_cache_put(file_hash: str, **fields: Any) -> None:
    parsed_json = fields["parsed_json"]
    warnings_json = fields.get("warnings_json")
    payload_bytes = len(parsed_json.encode("utf-8")) + len(
        (warnings_json or "").encode("utf-8")
    )
    if payload_bytes > config.MAX_PERIMETER_CACHE_BYTES:
        raise StorageLimitError(
            "Parsed perimeter exceeds the configured cache-entry limit."
        )
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM perimeter_cache WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if existing:
            # Same content hash means the versioned parse is identical. Touch
            # recency so maintenance cannot evict a cache hit in the small
            # window before its preview run row is created.
            conn.execute(
                "UPDATE perimeter_cache SET filename = ?, created_at = ? "
                "WHERE file_hash = ?",
                (fields.get("filename"), time.time(), file_hash),
            )
            conn.commit()
            return

        logical_bytes = _logical_storage_bytes(conn)
        row_count = conn.execute(
            "SELECT COUNT(*) FROM perimeter_cache"
        ).fetchone()[0]
        protected = _protected_perimeter_hashes(conn)
        while (logical_bytes + payload_bytes > config.DB_MAX_TOTAL_BYTES
               or row_count + 1 > config.PERIMETER_CACHE_MAX_ROWS):
            placeholders = ",".join("?" for _ in protected)
            predicate = (f"WHERE file_hash NOT IN ({placeholders})"
                         if protected else "")
            victim = conn.execute(
                "SELECT file_hash, "
                "COALESCE(length(CAST(parsed_json AS BLOB)), 0) + "
                "COALESCE(length(CAST(warnings_json AS BLOB)), 0) AS bytes "
                f"FROM perimeter_cache {predicate} "
                "ORDER BY created_at LIMIT 1",
                sorted(protected),
            ).fetchone()
            if victim is None:
                conn.rollback()
                raise StorageLimitError(
                    "Database has no room for another parsed perimeter."
                )
            conn.execute(
                "DELETE FROM perimeter_cache WHERE file_hash = ?",
                (victim["file_hash"],),
            )
            logical_bytes -= victim["bytes"] or 0
            row_count -= 1
        conn.execute(
            "INSERT INTO perimeter_cache (file_hash, filename, sheet, "
            "extraction_date, row_count, redbook_count, parsed_json, "
            "warnings_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_hash) DO UPDATE SET filename=excluded.filename",
            (file_hash, fields.get("filename"), fields.get("sheet"),
             fields.get("extraction_date"), fields.get("row_count"),
             fields.get("redbook_count"), fields["parsed_json"],
             warnings_json, time.time()),
        )
        conn.commit()


def _protected_perimeter_hashes(conn: sqlite3.Connection) -> set[str]:
    """Hashes whose removal would change a current or retryable run."""
    protected = {
        row["perimeter_hash"]
        for row in conn.execute(
            "SELECT DISTINCT perimeter_hash FROM runs "
            "WHERE status IN ('pending', 'queued', 'running', 'error') "
            "AND perimeter_hash IS NOT NULL"
        ).fetchall()
    }
    current = conn.execute(
        "SELECT value FROM settings WHERE key = 'current_perimeter'"
    ).fetchone()
    if current:
        try:
            current_hash = json.loads(current["value"])["hash"]
            if not isinstance(current_hash, str) or not current_hash:
                raise ValueError("empty perimeter hash")
            protected.add(current_hash)
        except (KeyError, TypeError, ValueError):
            logger.warning("current_perimeter setting is malformed")
    return protected


def perimeter_cache_prune(max_rows: int) -> int:
    """Keep recent perimeter parses without deleting active references."""
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM perimeter_cache"
        ).fetchone()[0]
        excess = max(0, count - max_rows)
        if excess:
            protected = _protected_perimeter_hashes(conn)
            placeholders = ",".join("?" for _ in protected)
            predicate = (f"WHERE file_hash NOT IN ({placeholders})"
                         if protected else "")
            changed = conn.execute(
                "DELETE FROM perimeter_cache WHERE file_hash IN ("
                "SELECT file_hash FROM perimeter_cache "
                f"{predicate} ORDER BY created_at LIMIT ?)",
                [*sorted(protected), excess],
            ).rowcount
            conn.commit()
            return changed
        return 0


def settings_get_many(keys: list[str]) -> dict[str, str]:
    """Values for the given settings keys (absent keys omitted) — one query,
    for callers probing many candidate keys at once."""
    if not keys:
        return {}
    with connect() as conn:
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            keys).fetchall()
    return {r["key"]: r["value"] for r in rows}


def setting_get(key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else None


def setting_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        conn.commit()


def setting_delete(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def override_clear(run_id: str, excel_row: int) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM overrides WHERE run_id = ? AND excel_row = ?",
            (run_id, excel_row),
        )
        conn.commit()


def overrides_for_run(run_id: str) -> dict[int, dict]:
    """Keyed by the PLOG sheet row — unique per run, unlike (CAMPAIGN, NO)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM overrides WHERE run_id = ?", (run_id,)
        ).fetchall()
    return {r["excel_row"]: dict(r) for r in rows}


# ------------------------------------------------------ storage maintenance

def database_storage_bytes() -> int:
    """Physical SQLite footprint, including WAL/shared-memory sidecars."""
    path = config.DB_PATH
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
        )
        if candidate.exists()
    )


def _logical_storage_bytes(conn: sqlite3.Connection) -> int:
    """Bytes in the large application-controlled SQLite payload columns."""
    expressions = (
        "SELECT COALESCE(SUM(length(CAST(result_json AS BLOB))), 0) + "
        "COALESCE(SUM(length(CAST(preview_json AS BLOB))), 0) FROM runs",
        "SELECT COALESCE(SUM(length(CAST(raw_json AS BLOB))), 0) "
        "FROM link_cache",
        "SELECT COALESCE(SUM(length(CAST(parsed_json AS BLOB))), 0) + "
        "COALESCE(SUM(length(CAST(warnings_json AS BLOB))), 0) "
        "FROM perimeter_cache",
        "SELECT COALESCE(SUM(length(CAST(value AS BLOB))), 0) FROM settings",
    )
    return sum(conn.execute(sql).fetchone()[0] for sql in expressions)


def database_logical_bytes() -> int:
    with connect() as conn:
        return _logical_storage_bytes(conn)


def active_run_count() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')"
        ).fetchone()[0]


def healthcheck() -> bool:
    """Cheap liveness/readability check used by the ASGI health endpoint."""
    try:
        with connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except sqlite3.Error:
        logger.exception("database health check failed")
        return False


def database_maintenance(*, allow_full_vacuum: bool = False) -> dict[str, int]:
    """Prune caches, checkpoint WAL, and reclaim safely available pages.

    Full VACUUM is reserved for an idle process and only attempted when the
    filesystem has enough room for SQLite's replacement file. New databases
    use incremental auto-vacuum, avoiding that expensive path in steady state.
    """
    cache = cache_prune(config.LINK_CACHE_MAX_ROWS,
                        config.LINK_CACHE_MAX_RAW_BYTES)
    perimeter_rows = perimeter_cache_prune(config.PERIMETER_CACHE_MAX_ROWS)
    vacuumed = 0
    with connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if free_pages and auto_vacuum == 2:
            conn.execute(f"PRAGMA incremental_vacuum({min(free_pages, 2000)})")
            vacuumed = min(free_pages, 2000)

    if (allow_full_vacuum and page_count and free_pages / page_count >= 0.25
            and config.DB_PATH.exists()):
        size = config.DB_PATH.stat().st_size
        free_disk = shutil.disk_usage(config.DATA_DIR).free
        if free_disk > size * 2:
            with connect() as conn:
                conn.execute("VACUUM")
            vacuumed = free_pages
        else:
            logger.warning(
                "skipping SQLite VACUUM: need=%d free=%d", size * 2, free_disk
            )
    return {
        "cache_rows": cache["rows"],
        "cache_payloads": cache["payloads"],
        "perimeter_rows": perimeter_rows,
        "vacuumed_pages": vacuumed,
    }
