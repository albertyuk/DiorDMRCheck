"""Regression tests for storage, migration, and numeric hardening."""
from __future__ import annotations

import asyncio
import math
import sqlite3
import threading

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hardening.sqlite3")
    return tmp_path


def test_cache_failure_cannot_downgrade_success(isolated_db):
    from app.core import db

    db.cache_put("https://example/note", status="ok", note_id="note-1",
                 author_id="author-1", source="direct")
    db.cache_put("https://example/note", status="failed",
                 error="transient timeout")

    row = db.cache_get("https://example/note")
    assert row["status"] == "ok"
    assert row["note_id"] == "note-1"
    assert row["author_id"] == "author-1"


def test_cache_pruning_strips_payloads_then_failed_rows(isolated_db):
    from app.core import db

    db.cache_put("https://example/ok", status="ok", note_id="one",
                 raw_json="x" * 20, resolved_at=1)
    db.cache_put("https://example/failed", status="failed", error="timeout",
                 raw_json="y" * 20, resolved_at=2)
    result = db.cache_prune(max_rows=1, max_raw_bytes=0)

    assert result == {"rows": 1, "payloads": 2}
    assert db.cache_get("https://example/ok")["raw_json"] is None
    assert db.cache_get("https://example/failed") is None


def test_perimeter_pruning_preserves_current_entry(isolated_db):
    import json
    from app.core import db

    for key in ("current", "other"):
        db.perimeter_cache_put(
            key, parsed_json='{"rows": []}', filename=f"{key}.xlsx"
        )
    db.setting_set("current_perimeter", json.dumps({"hash": "current"}))

    assert db.perimeter_cache_prune(1) == 1
    assert db.perimeter_cache_get("current") is not None
    assert db.perimeter_cache_get("other") is None


def test_perimeter_put_enforces_total_db_cap_immediately(
        isolated_db, monkeypatch):
    from app import config
    from app.core import db

    monkeypatch.setattr(config, "MAX_PERIMETER_CACHE_BYTES", 1_000)
    monkeypatch.setattr(config, "DB_MAX_TOTAL_BYTES", 35)
    monkeypatch.setattr(config, "PERIMETER_CACHE_MAX_ROWS", 100)
    db.perimeter_cache_put("old", parsed_json="x" * 30)
    db.perimeter_cache_put("new", parsed_json="y" * 30)

    assert db.perimeter_cache_get("old") is None
    assert db.perimeter_cache_get("new") is not None


def test_perimeter_put_never_evicts_retryable_run(
        isolated_db, monkeypatch):
    from app import config
    from app.core import db

    monkeypatch.setattr(config, "MAX_PERIMETER_CACHE_BYTES", 1_000)
    monkeypatch.setattr(config, "DB_MAX_TOTAL_BYTES", 40)
    monkeypatch.setattr(config, "PERIMETER_CACHE_MAX_ROWS", 100)
    db.perimeter_cache_put("retry", parsed_json="x" * 30)
    db.run_create("failed", plog_path="p", dmr_path="d",
                  perimeter_hash="retry", perimeter_uploaded=True)
    db.run_update("failed", status="error")

    with pytest.raises(db.StorageLimitError, match="no room"):
        db.perimeter_cache_put("new", parsed_json="y" * 30)
    assert db.perimeter_cache_get("retry") is not None


def test_concurrent_cache_enrichment_does_not_lose_fields(isolated_db):
    from app.core import db

    url = "https://example/note"
    db.cache_put(url, status="ok", note_id="note-1", source="direct")
    barrier = threading.Barrier(3)

    def merge(**fields):
        barrier.wait()
        db.cache_merge(url, **fields)

    threads = [
        threading.Thread(target=merge, kwargs={"author_id": "author-1"}),
        threading.Thread(target=merge, kwargs={"likes": 42}),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    row = db.cache_get(url)
    assert row["author_id"] == "author-1"
    assert row["likes"] == 42


def test_legacy_override_migration_preserves_and_maps_rows(tmp_path):
    from app.core import migrations

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        migrations.apply(conn)
        conn.execute("DROP TABLE overrides")
        conn.execute("""
            CREATE TABLE overrides (
                run_id TEXT NOT NULL, campaign TEXT NOT NULL,
                no TEXT NOT NULL, status TEXT NOT NULL, note TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (run_id, campaign, no)
            )
        """)
        result = ('{"verdicts":[{"campaign":"Summer","no":"7",'
                  '"excel_row":23}]}')
        conn.execute(
            "INSERT INTO runs (id, created_at, status, result_json) "
            "VALUES ('run-1', 1, 'done', ?)", (result,)
        )
        conn.execute(
            "INSERT INTO overrides VALUES "
            "('run-1', 'Summer', '7', 'MATCH', 'checked', 2)"
        )
        conn.execute(
            "INSERT INTO overrides VALUES "
            "('missing-run', 'Old', '9', 'REVIEW', 'recover me', 3)"
        )
        conn.commit()

        migrations.apply(conn)
        rows = conn.execute(
            "SELECT run_id, excel_row, campaign, no, status, note "
            "FROM overrides ORDER BY run_id"
        ).fetchall()
        assert rows == [
            ("missing-run", -1, "Old", "9", "REVIEW", "recover me"),
            ("run-1", 23, "Summer", "7", "MATCH", "checked"),
        ]
        assert conn.execute(
            "SELECT value FROM settings WHERE key = "
            "'migration:legacy_overrides_unmapped'"
        ).fetchone() == ("1",)


def test_migration_does_not_swallow_unrelated_operational_error(
        tmp_path, monkeypatch):
    from app.core import migrations

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(migrations, "_add_column", fail)
    with sqlite3.connect(tmp_path / "broken.sqlite3") as conn:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            migrations.apply(conn)


def test_run_result_size_is_enforced(isolated_db, monkeypatch):
    from app import config
    from app.core import db

    monkeypatch.setattr(config, "MAX_RESULT_BYTES", 10)
    monkeypatch.setattr(config, "MAX_RESULT_MB", 1)
    db.run_create("too-large", plog_path="p", dmr_path="d")
    with pytest.raises(db.StorageLimitError, match="result exceeds"):
        db.run_update("too-large", result_json="x" * 11)


def test_numeric_coercion_rejects_non_finite_values():
    from app.core.xlsx import to_float, to_int

    for value in (math.nan, math.inf, -math.inf, "nan", "Infinity", "-inf"):
        assert to_float(value) is None
        assert to_int(value) is None


def test_header_scan_does_not_materialize_sparse_xfd_cells():
    from openpyxl import Workbook
    from app.core.xlsx import find_header_row

    workbook = Workbook()
    sheet = workbook.active
    sheet["XFD1"] = "far-away metadata"
    sheet["A2"] = "NAME"
    sheet["B2"] = "POST LINK"
    populated_before = len(sheet._cells)

    row, columns = find_header_row(sheet, {"name", "postlink"})

    assert row == 2
    assert columns["name"] == 1
    assert columns["postlink"] == 2
    assert len(sheet._cells) == populated_before


def test_cross_deleting_admins_cannot_remove_every_admin(isolated_db):
    from app.core import db
    from app.auth.service import hash_password

    db.user_upsert("admin-a", hash_password("password-a"), is_admin=True)
    db.user_upsert("admin-b", hash_password("password-b"), is_admin=True)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def remove(actor, target):
        barrier.wait()
        outcomes.append(db.user_delete_guarded(actor, target))

    threads = [
        threading.Thread(target=remove, args=("admin-a", "admin-b")),
        threading.Thread(target=remove, args=("admin-b", "admin-a")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes.count("deleted") == 1
    assert db.admin_count() == 1


def test_maintenance_loop_logs_failure_and_keeps_running(monkeypatch):
    import app.main as main

    class StopLoop(BaseException):
        pass

    calls = 0

    def cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient cleanup failure")
        raise StopLoop

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(main, "_maintenance_once", cycle)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    main._maintenance_state["consecutive_failures"] = 0
    with pytest.raises(StopLoop):
        asyncio.run(main._maintenance_loop())
    assert calls == 2
    assert main._maintenance_state["consecutive_failures"] == 1


def test_lifespan_closes_tikhub_pool(monkeypatch):
    import app.main as main

    closed = []
    monkeypatch.setattr(main.config, "validate_runtime", lambda: None)
    monkeypatch.setattr(main.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(main.runs, "recover_orphans", lambda: None)
    monkeypatch.setattr(main, "_maintenance_once", lambda: None)
    monkeypatch.setattr(
        main.links, "close_tikhub_client", lambda: closed.append(True)
    )

    async def exercise():
        async with main.lifespan(None):
            pass

    asyncio.run(exercise())
    assert closed == [True]
