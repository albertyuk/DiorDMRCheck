"""SQLite schema and migrations, applied explicitly per database.

Kept apart from the connection factory so tests and startup control when
schema work happens; ``apply`` is idempotent (CREATE IF NOT EXISTS plus
additive ALTERs that ignore already-exists errors).
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA_VERSION = 2

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
    resolved_at  REAL NOT NULL,
    author_failed_at REAL                 -- TTL marker for failed author enrichment
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
    error         TEXT,
    perimeter_hash TEXT,
    perimeter_uploaded INTEGER DEFAULT 0,
    perimeter_name TEXT
);

CREATE TABLE IF NOT EXISTS overrides (
    run_id     TEXT NOT NULL,
    excel_row  INTEGER NOT NULL,          -- unique per run even when (CAMPAIGN, NO) collides
    campaign   TEXT NOT NULL,
    no         TEXT NOT NULL,
    status     TEXT NOT NULL,
    note       TEXT,
    updated_by TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, excel_row)
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,       -- stored casefolded
    display       TEXT,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS perimeter_cache (
    file_hash       TEXT PRIMARY KEY,     -- sha256 of the uploaded workbook
    filename        TEXT,
    sheet           TEXT,
    extraction_date TEXT,
    row_count       INTEGER,
    redbook_count   INTEGER,
    parsed_json     TEXT NOT NULL,        -- rows with precomputed norm forms
    warnings_json   TEXT,                 -- parse warnings, replayed on cache hits
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def apply(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema on an open connection. Idempotent."""
    # Incremental auto-vacuum must be selected before the first table is
    # created. Existing databases retain their current setting and can still
    # be compacted explicitly by core.db.database_maintenance.
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
    ).fetchone()
    if not has_tables:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.executescript(SCHEMA)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _add_column(conn, "link_cache", "author_failed_at", "REAL")
        _add_column(conn, "overrides", "updated_by", "TEXT")
        _add_column(conn, "runs", "perimeter_hash", "TEXT")
        # Existing rows cannot distinguish an explicit perimeter upload from
        # an inherited default, so preserve NULL/unknown for legacy rows.
        _add_column(conn, "runs", "perimeter_uploaded", "INTEGER")
        _add_column(conn, "runs", "perimeter_name", "TEXT")
        _add_column(conn, "perimeter_cache", "warnings_json", "TEXT")
        _migrate_legacy_overrides(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str,
                declaration: str) -> None:
    """Add a known schema column without swallowing unrelated DB errors."""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _result_excel_rows(conn: sqlite3.Connection, run_id: str,
                       campaign: str, no: str) -> list[int]:
    row = conn.execute(
        "SELECT result_json FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        result = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    matches: list[int] = []
    for verdict in result.get("verdicts", []):
        if (str(verdict.get("campaign", "")) == campaign
                and str(verdict.get("no", "")) == no):
            try:
                matches.append(int(verdict["excel_row"]))
            except (KeyError, TypeError, ValueError):
                continue
    return matches


def _migrate_legacy_overrides(conn: sqlite3.Connection) -> None:
    """Preserve pre-excel-row overrides instead of dropping them.

    Where the finished run JSON provides an unambiguous row, the override is
    restored onto that row. Otherwise it receives a unique negative row key:
    the evidence remains queryable/exportable for manual recovery rather than
    being silently destroyed by startup.
    """
    cols = _columns(conn, "overrides")
    if not cols or "excel_row" in cols:
        return

    legacy_rows = [dict(zip(
        [column[0] for column in cursor.description], row
    )) for cursor in [conn.execute("SELECT * FROM overrides")]
        for row in cursor.fetchall()]
    conn.execute("ALTER TABLE overrides RENAME TO overrides_legacy_v0")
    conn.execute("""
        CREATE TABLE overrides (
            run_id TEXT NOT NULL,
            excel_row INTEGER NOT NULL,
            campaign TEXT NOT NULL,
            no TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            updated_by TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (run_id, excel_row)
        )
    """)

    next_fallback: dict[str, int] = {}
    unresolved = 0
    for old in legacy_rows:
        run_id = str(old.get("run_id") or "")
        campaign = str(old.get("campaign") or "")
        no = str(old.get("no") or "")
        candidates = _result_excel_rows(conn, run_id, campaign, no)
        if len(candidates) == 1:
            excel_row = candidates[0]
        else:
            excel_row = next_fallback.get(run_id, -1)
            next_fallback[run_id] = excel_row - 1
            unresolved += 1
        conn.execute(
            "INSERT INTO overrides (run_id, excel_row, campaign, no, status, "
            "note, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, excel_row, campaign, no, str(old.get("status") or ""),
             old.get("note"), old.get("updated_by"),
             float(old.get("updated_at") or time.time())),
        )
    conn.execute("DROP TABLE overrides_legacy_v0")
    if unresolved:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("migration:legacy_overrides_unmapped", str(unresolved)),
        )
