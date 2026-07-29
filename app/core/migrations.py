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
    perimeter_hash TEXT,
    perimeter_uploaded INTEGER DEFAULT 0,
    perimeter_name TEXT,
    perimeter_macro_hash TEXT,
    perimeter_macro_uploaded INTEGER DEFAULT 0,
    perimeter_macro_name TEXT
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
    username       TEXT PRIMARY KEY,      -- stored casefolded
    display        TEXT,
    password_hash  TEXT NOT NULL,         -- '' until an invite is accepted
    is_admin       INTEGER DEFAULT 0,
    created_at     REAL NOT NULL,
    email          TEXT,                  -- lowercased; NULL for legacy rows
    email_verified INTEGER DEFAULT 0
);

-- Single-use, time-limited invite / password-reset tokens. Only the SHA-256
-- of the raw token is stored, so a DB read cannot mint a working link.
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    purpose    TEXT NOT NULL,             -- 'invite' | 'reset'
    email      TEXT,                      -- address the link was sent to
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(username);

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
        # Existing rows cannot distinguish an explicit perimeter upload from
        # an inherited default. Preserve that as NULL/unknown so first-start
        # handling can retain the previous release's promotion behavior;
        # newly created runs always store an explicit 0 or 1.
        "ALTER TABLE runs ADD COLUMN perimeter_uploaded INTEGER",
        "ALTER TABLE runs ADD COLUMN perimeter_name TEXT",
        "ALTER TABLE perimeter_cache ADD COLUMN warnings_json TEXT",
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN perimeter_macro_hash TEXT",
        "ALTER TABLE runs ADD COLUMN perimeter_macro_uploaded INTEGER",
        "ALTER TABLE runs ADD COLUMN perimeter_macro_name TEXT",
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
