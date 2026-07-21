"""core.db: SQL-assembly guards."""
from __future__ import annotations

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
