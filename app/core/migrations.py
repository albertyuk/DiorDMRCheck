"""SQLite schema and migrations, applied explicitly per database.

Kept apart from the connection factory so tests and startup control when
schema work happens; ``apply`` is idempotent (CREATE IF NOT EXISTS plus
additive ALTERs that ignore already-exists errors).
"""
from __future__ import annotations

import sqlite3

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
    perimeter_hash TEXT
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
    conn.executescript(SCHEMA)
    # additive migrations for databases created by older versions
    for stmt in (
        "ALTER TABLE link_cache ADD COLUMN author_failed_at REAL",
        "ALTER TABLE overrides ADD COLUMN updated_by TEXT",
        "ALTER TABLE runs ADD COLUMN perimeter_hash TEXT",
        "ALTER TABLE perimeter_cache ADD COLUMN warnings_json TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    # pre-release overrides table was keyed (run_id, campaign, no);
    # rebuild it keyed by excel_row (no deployments existed yet)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(overrides)")}
    if cols and "excel_row" not in cols:
        conn.execute("DROP TABLE overrides")
        conn.executescript(SCHEMA)
    conn.commit()
