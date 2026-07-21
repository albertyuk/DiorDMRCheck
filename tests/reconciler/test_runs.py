"""Run pool: concurrency cap, FIFO overflow, per-run idempotence."""
from __future__ import annotations

import threading
import time

import pytest

from app import config
from app.reconciler import runs


@pytest.fixture
def pool(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(config, "RUN_MAX_CONCURRENT", 1)
    runs._active.clear()
    runs._pending.clear()
    yield
    runs._active.clear()
    runs._pending.clear()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_cap_defers_and_then_runs_fifo(pool, monkeypatch):
    from app.core import db
    for rid in ("r1", "r2", "r3"):
        db.run_create(rid, plog_path="p", dmr_path="d")

    release = threading.Event()
    ran: list[str] = []

    def fake_run(run_id):
        ran.append(run_id)
        release.wait(5)

    monkeypatch.setattr(runs, "_run", fake_run)
    runs.start_run("r1")
    assert _wait_for(lambda: ran == ["r1"])
    runs.start_run("r2")
    runs.start_run("r3")
    time.sleep(0.05)
    assert ran == ["r1"]                          # capped at 1
    assert list(runs._pending) == ["r2", "r3"]
    assert "Waiting for a free run slot" in db.run_get("r2")["message"]

    release.set()                                  # r1 finishes → r2, then r3
    assert _wait_for(lambda: ran == ["r1", "r2", "r3"])
    assert _wait_for(lambda: not runs._active and not runs._pending)


def test_start_run_is_idempotent_per_run(pool, monkeypatch):
    started: list[str] = []
    gate = threading.Event()

    def fake_run(run_id):
        started.append(run_id)
        gate.wait(5)

    monkeypatch.setattr(runs, "_run", fake_run)
    runs.start_run("dup")
    assert _wait_for(lambda: started == ["dup"])
    runs.start_run("dup")                          # active → ignored
    time.sleep(0.05)
    assert started == ["dup"] and not runs._pending
    gate.set()
    assert _wait_for(lambda: not runs._active)
