"""core.db: SQL-assembly guards."""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")


def test_run_update_rejects_unknown_columns(isolated_db):
    """run_update interpolates column names into SQL — anything outside the
    whitelist must raise instead of executing."""
    from app.core import db
    db.run_create("r1", plog_path="p", dmr_path="d")
    with pytest.raises(ValueError, match="unknown column"):
        db.run_update("r1", **{"status = 'x' WHERE 1=1; --": "boom"})
    with pytest.raises(ValueError, match="unknown column"):
        db.run_update("r1", created_at=0)          # immutable column
    db.run_update("r1", status="queued")           # whitelisted still works
    assert db.run_get("r1")["status"] == "queued"


def test_run_bump_counter_rejects_unknown_column(isolated_db):
    from app.core import db
    db.run_create("r2", plog_path="p", dmr_path="d")
    with pytest.raises(ValueError, match="unknown counter"):
        db.run_bump_counter("r2", "status")
    db.run_bump_counter("r2", "tikhub_calls")
    assert db.run_get("r2")["tikhub_calls"] == 1


def test_run_create_records_perimeter_provenance(isolated_db):
    from app.core import db
    db.run_create(
        "r3", plog_path="p", dmr_path="d", perimeter_hash="abc",
        perimeter_uploaded=True, perimeter_name="selected.xlsx")
    run = db.run_get("r3")
    assert run["perimeter_hash"] == "abc"
    assert run["perimeter_uploaded"] == 1
    assert run["perimeter_name"] == "selected.xlsx"


def test_existing_runs_table_migrates_perimeter_provenance(tmp_path,
                                                            monkeypatch):
    """Databases created by the previous release gain both provenance fields
    without rebuilding or losing the runs table."""
    from app import config
    from app.core import db

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                status TEXT NOT NULL, phase TEXT, progress_done INTEGER,
                progress_total INTEGER, message TEXT, plog_path TEXT,
                dmr_path TEXT, plog_name TEXT, dmr_name TEXT,
                options_json TEXT, preview_json TEXT, result_json TEXT,
                summary_json TEXT, tikhub_calls INTEGER, llm_calls INTEGER,
                error TEXT, perimeter_hash TEXT
            )
        """)
        conn.execute(
            "INSERT INTO runs (id, created_at, status, perimeter_hash) "
            "VALUES ('old', 1, 'pending', 'hash')")

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", path)
    run = db.run_get("old")
    assert run["perimeter_hash"] == "hash"
    # The old schema recorded only a hash, so provenance is unknowable. NULL
    # lets first-start behavior remain backward-compatible while every new
    # run records an explicit 0 or 1.
    assert run["perimeter_uploaded"] is None
    assert run["perimeter_name"] is None


# to_date lives in core.xlsx — YY/MM/DD trackers ("24/11/27") must parse

def test_to_date_accepts_two_digit_year_first():
    from datetime import date
    from app.core.xlsx import to_date
    assert to_date("24/11/27") == date(2024, 11, 27)
    assert to_date("24-11-23") == date(2024, 11, 23)
    assert to_date("24.11.19") == date(2024, 11, 19)


def test_to_date_existing_interpretations_unchanged():
    from datetime import date
    from app.core.xlsx import to_date
    # Ambiguous strings keep their historical month-first reading, regardless
    # of separator. The old implementation changed the date by twenty years
    # when "/" was replaced with "-" or ".".
    for value in ("05/06/25", "05-06-25", "05.06.25"):
        assert to_date(value) == date(2025, 5, 6)
    assert to_date("12/31/2025") == date(2025, 12, 31)  # %m/%d/%Y
    assert to_date("2025-06-01") == date(2025, 6, 1)
    assert to_date("garbage") is None


def test_to_date_supports_explicit_source_order():
    from datetime import date
    from app.core.xlsx import to_date

    assert to_date("05/06/25", date_order="year-first") == date(2005, 6, 25)
    assert to_date("05/06/2025", date_order="day-first") == date(2025, 6, 5)
    assert to_date("05/06/2025", date_order="month-first") == date(2025, 5, 6)
    with pytest.raises(ValueError, match="date_order"):
        to_date("2025-01-01", date_order="guess")
