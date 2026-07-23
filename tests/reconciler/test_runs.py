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
    runs._restart_pending.clear()
    yield
    runs._active.clear()
    runs._pending.clear()
    runs._restart_pending.clear()


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


def test_retry_requested_during_error_teardown_is_handed_off(pool, monkeypatch):
    """A retry can arrive after _run persisted ``error`` but before its slot
    drops the run id from the active registry. It must be queued for handoff,
    not ignored and left permanently ``queued`` in SQLite."""
    from app.core import db

    db.run_create("retry", plog_path="p", dmr_path="d")
    db.run_update("retry", status="queued")
    first_errored = threading.Event()
    release_first = threading.Event()
    attempts: list[str] = []

    def fake_run(run_id):
        attempts.append(run_id)
        if len(attempts) == 1:
            db.run_update(run_id, status="error", phase="error")
            first_errored.set()
            release_first.wait(5)
        else:
            db.run_update(run_id, status="done", phase="done")

    monkeypatch.setattr(runs, "_run", fake_run)
    runs.start_run("retry")
    assert first_errored.wait(2)

    # Mirrors POST /runs/{id}/start: the retry flips status back to queued
    # while the failing worker still owns the in-process slot.
    db.run_update("retry", status="queued", error=None)
    runs.start_run("retry")
    assert runs._restart_pending == {"retry"}

    release_first.set()
    assert _wait_for(lambda: attempts == ["retry", "retry"])
    assert _wait_for(lambda: not runs._active and not runs._pending)


def test_retry_waits_for_its_own_generation_with_multiple_slots(pool,
                                                                monkeypatch):
    """A different finishing slot must not start an ID whose old worker is
    still active; otherwise the old teardown can erase the retry's registry
    entry and let the physical worker count exceed the cap."""
    from app.core import db

    monkeypatch.setattr(config, "RUN_MAX_CONCURRENT", 2)
    for run_id in ("retry", "other"):
        db.run_create(run_id, plog_path="p", dmr_path="d")
        db.run_update(run_id, status="queued")

    first_errored = threading.Event()
    release_first = threading.Event()
    release_other = threading.Event()
    retry_started = threading.Event()
    release_retry = threading.Event()
    attempts = 0

    def fake_run(run_id):
        nonlocal attempts
        if run_id == "other":
            release_other.wait(5)
            return
        attempts += 1
        if attempts == 1:
            db.run_update(run_id, status="error", phase="error")
            first_errored.set()
            release_first.wait(5)
        else:
            retry_started.set()
            release_retry.wait(5)

    monkeypatch.setattr(runs, "_run", fake_run)
    runs.start_run("retry")
    runs.start_run("other")
    assert first_errored.wait(2)

    db.run_update("retry", status="queued", error=None)
    runs.start_run("retry")
    assert runs._restart_pending == {"retry"}

    release_other.set()
    assert _wait_for(lambda: "other" not in runs._active)
    assert not retry_started.is_set()
    assert "retry" in runs._active

    release_first.set()
    assert retry_started.wait(2)
    assert "retry" in runs._active
    release_retry.set()
    assert _wait_for(lambda: not runs._active and not runs._pending
                     and not runs._restart_pending)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_run_concurrency_must_be_positive(monkeypatch, value):
    monkeypatch.setenv("RUN_MAX_CONCURRENT", value)
    with pytest.raises(ValueError, match=r"RUN_MAX_CONCURRENT must be >= 1"):
        config._positive_int_env("RUN_MAX_CONCURRENT", "2")


# ------------------------------------------------ editable export window

def test_apply_window_override():
    from datetime import date
    from app.reconciler.parsers import parse_dmr
    from app.reconciler.runs import apply_window_override

    class FakeDmr:
        window_from = date(2025, 1, 1)
        window_to = date(2025, 12, 31)

    d = FakeDmr()
    # no keys at all (legacy options) → detected window untouched
    apply_window_override(d, {"use_llm": True})
    assert d.window_from == date(2025, 1, 1)
    # BOTH bounds empty (a pre-feature run retried through the error panel) →
    # KEEP the detected window, never silently disable the check
    apply_window_override(d, {"window_from": "", "window_to": ""})
    assert d.window_from == date(2025, 1, 1) and d.window_to == date(2025, 12, 31)
    # garbage on both → also a no-op, detected window kept
    apply_window_override(d, {"window_from": "not-a-date", "window_to": "x"})
    assert d.window_from == date(2025, 1, 1)
    # user widened the window to include 2024
    apply_window_override(d, {"window_from": "2024-01-01",
                              "window_to": "2025-12-31"})
    assert d.window_from == date(2024, 1, 1)
    assert d.window_to == date(2025, 12, 31)
    # one real bound set, the other cleared → the set side applies, the
    # cleared side unsets (an explicit half-open edit)
    apply_window_override(d, {"window_from": "", "window_to": "2025-06-30"})
    assert d.window_from is None and d.window_to == date(2025, 6, 30)
