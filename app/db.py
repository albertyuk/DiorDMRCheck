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

from . import config

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS link_cache (
    url          TEXT PRIMARY KEY,
    status       TEXT NOT NULL,           -- ok | failed
    note_id      TEXT,
    author_id    TEXT,
    author_name  TEXT,
    likes        INTEGER,
    collects     INTEGER,
    comments     INTEGER,
    title        TEXT,
    publish_time TEXT,
    source       TEXT,                    -- direct | tikhub | direct+tikhub
    error        TEXT,
    raw_json     TEXT,
    resolved_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_cache_note ON link_cache(note_id);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL,          -- pending | running | done | error
    phase         TEXT,
    progress_done INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    message       TEXT,
    plog_path     TEXT,
    dmr_path      TEXT,
    plog_name     TEXT,
    dmr_name      TEXT,
    options_json  TEXT,
    preview_json  TEXT,
    result_json   TEXT,
    summary_json  TEXT,
    tikhub_calls  INTEGER DEFAULT 0,
    llm_calls     INTEGER DEFAULT 0,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS overrides (
    run_id     TEXT NOT NULL,
    campaign   TEXT NOT NULL,
    no         TEXT NOT NULL,
    status     TEXT NOT NULL,
    note       TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, campaign, no)
);
"""


def connect() -> sqlite3.Connection:
    global _initialized
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if not _initialized:
        with _init_lock:
            if not _initialized:
                conn.executescript(SCHEMA)
                conn.commit()
                _initialized = True
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
        "resolved_at",
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
    """Update only the provided fields on an existing cache row."""
    existing = cache_get(url) or {}
    existing.pop("url", None)
    existing.update({k: v for k, v in fields.items() if v is not None})
    cache_put(url, **existing)


# ---------------------------------------------------------------------- runs

def run_create(run_id: str, **fields: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, created_at, status, plog_path, dmr_path, "
            "plog_name, dmr_name, options_json, preview_json) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                run_id, time.time(),
                fields.get("plog_path"), fields.get("dmr_path"),
                fields.get("plog_name"), fields.get("dmr_name"),
                json.dumps(fields.get("options") or {}),
                json.dumps(fields.get("preview") or {}, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()


def run_update(run_id: str, **fields: Any) -> None:
    if not fields:
        return
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


def run_progress(run_id: str, phase: str, done: int, total: int, message: str) -> None:
    run_update(run_id, phase=phase, progress_done=done, progress_total=total,
               message=message)


def run_bump_counter(run_id: str, column: str, amount: int = 1) -> None:
    assert column in ("tikhub_calls", "llm_calls")
    with connect() as conn:
        conn.execute(
            f"UPDATE runs SET {column} = COALESCE({column}, 0) + ? WHERE id = ?",
            (amount, run_id),
        )
        conn.commit()


# ----------------------------------------------------------------- overrides

def override_set(run_id: str, campaign: str, no: str, status: str, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO overrides (run_id, campaign, no, status, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, campaign, no) DO UPDATE SET "
            "status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
            (run_id, campaign, no, status, note, time.time()),
        )
        conn.commit()


def override_clear(run_id: str, campaign: str, no: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM overrides WHERE run_id = ? AND campaign = ? AND no = ?",
            (run_id, campaign, no),
        )
        conn.commit()


def overrides_for_run(run_id: str) -> dict[tuple[str, str], dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM overrides WHERE run_id = ?", (run_id,)
        ).fetchall()
    return {(r["campaign"], r["no"]): dict(r) for r in rows}
