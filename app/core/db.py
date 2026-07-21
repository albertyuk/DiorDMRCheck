"""SQLite persistence: link cache, runs, human overrides.

Connections are opened per call (WAL mode) so the background run thread and
request handlers never share a connection.
"""
from __future__ import annotations

import json
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

def cache_get(url: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM link_cache WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def cache_put(url: str, **fields: Any) -> None:
    fields.setdefault("resolved_at", time.time())
    cols = [
        "status", "note_id", "author_id", "author_name", "likes", "collects",
        "comments", "title", "publish_time", "source", "error", "raw_json",
        "resolved_at", "author_failed_at",
    ]
    values = [fields.get(c) for c in cols]
    with connect() as conn:
        conn.execute(
            f"INSERT INTO link_cache (url, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)}) "
            "ON CONFLICT(url) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols),
            [url, *values],
        )
        conn.commit()


def cache_merge(url: str, **fields: Any) -> None:
    """Update the provided fields (verbatim — an explicit None clears the
    field) on an existing cache row, leaving all other fields untouched."""
    existing = cache_get(url) or {}
    existing.pop("url", None)
    existing.update(fields)
    cache_put(url, **existing)


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
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
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


def user_set_password(username: str, password_hash: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                     (password_hash, username))
        conn.commit()


def user_delete(username: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()


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
    with connect() as conn:
        conn.execute(
            "INSERT INTO perimeter_cache (file_hash, filename, sheet, "
            "extraction_date, row_count, redbook_count, parsed_json, "
            "warnings_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_hash) DO UPDATE SET filename=excluded.filename",
            (file_hash, fields.get("filename"), fields.get("sheet"),
             fields.get("extraction_date"), fields.get("row_count"),
             fields.get("redbook_count"), fields["parsed_json"],
             fields.get("warnings_json"), time.time()),
        )
        conn.commit()


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
