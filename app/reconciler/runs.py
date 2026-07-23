"""Background run orchestration.

Each run executes in a daemon thread drawn from a small bounded pool
(config.RUN_MAX_CONCURRENT): excess starts stay 'queued' and begin FIFO as
slots free up, so N users cannot spawn N CPU/API-heavy runs at once.
Progress and results are persisted in SQLite so the web layer only ever
polls the database. Partial failure is normal — a dead link, a TikHub 4xx,
a rate limit each become per-row status, and the run completes. Only
file-level parse failures abort a run.
"""
from __future__ import annotations

import json
import threading
import traceback
from collections import deque

from .. import config
from ..core import db
from ..core.uploads import run_upload_task_sync
from . import perimeter as perimeter_mod
from .adjudicator import adjudicate, summarize_run
from .domain import ENGAGEMENT_CAVEAT
from .pipeline import run_pipeline, status_counts, summary_buckets
from .parsers import parse_dmr, parse_plog
from .reverse_audit import reverse_audit


def recover_orphans() -> None:
    """Mark runs orphaned by a restart as errors. A deploy/restart (or Fly
    auto-stop) kills in-flight daemon threads; their runs would otherwise
    stay 'running' forever with no restart path. Called at app startup."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE runs SET status='error', phase='error', "
            "message='Run interrupted by a restart — use Retry.' "
            "WHERE status='running' OR status='queued'"
        )
        conn.commit()


# Bounded worker pool: registry of in-flight runs + FIFO of deferred ones.
# Daemon threads on purpose — shutdown semantics are unchanged (a restart
# kills in-flight runs; recover_orphans marks them at next startup).
_pool_lock = threading.Lock()
_active: set[str] = set()
_pending: deque[str] = deque()
# Retries requested during an errored worker's teardown stay attached to that
# physical slot. Putting them in the shared FIFO lets a different finishing
# slot start the retry while the old generation is still active.
_restart_pending: set[str] = set()
QUEUED_MESSAGE = "Waiting for a free run slot…"


def _defer_locked(run_id: str) -> None:
    """Persist and enqueue a deferred run while ``_pool_lock`` is held.

    Persisting first prevents a finishing slot from starting the run before
    the waiting message is written (which could otherwise overwrite the new
    worker's initial "Parsing workbooks…" progress).
    """
    db.run_update(run_id, message=QUEUED_MESSAGE)
    _pending.append(run_id)


def start_run(run_id: str) -> None:
    """Start a run, or queue it when the pool is full. Idempotent per run:
    a run already active or pending is never double-started.

    One exception is an explicit retry requested after the active worker has
    persisted its terminal error but before ``_run_slot`` has removed the run
    from ``_active``. The web route has already changed that row back to
    ``queued`` while its phase remains ``error``; enqueue it once so the
    finishing slot hands off to the retry instead of stranding the DB row.
    """
    with _pool_lock:
        if run_id in _pending or run_id in _restart_pending:
            return
        if run_id in _active:
            run = db.run_get(run_id)
            if (run and run.get("status") == "queued"
                    and run.get("phase") == "error"):
                db.run_update(run_id, message=QUEUED_MESSAGE)
                _restart_pending.add(run_id)
            return
        if len(_active) >= config.RUN_MAX_CONCURRENT:
            _defer_locked(run_id)
            return
        _active.add(run_id)
    threading.Thread(target=_run_slot, args=(run_id,), daemon=True).start()


def _run_slot(run_id: str) -> None:
    try:
        _run(run_id)
    finally:
        with _pool_lock:
            _active.discard(run_id)
            next_id = None
            if run_id in _restart_pending:
                # Only the old generation's own teardown may hand this ID to
                # its retry generation; no other slot can start it early.
                _restart_pending.discard(run_id)
                next_id = run_id
                _active.add(next_id)
            elif _pending and len(_active) < config.RUN_MAX_CONCURRENT:
                next_id = _pending.popleft()
                _active.add(next_id)
        if next_id:
            threading.Thread(target=_run_slot, args=(next_id,),
                             daemon=True).start()


def apply_window_override(dmr, options: dict) -> None:
    """Apply the user-edited DMR export window from the confirm screen.

    The form prefills from the metadata-detected window. The override applies
    only when at least ONE bound is a real date — so an empty submission (a
    pre-feature run retried through the error panel, whose hidden inputs post
    blank strings) is a no-op that KEEPS the detected window rather than
    silently disabling the out-of-window checks. To widen or narrow the
    window, set the dates; to effectively disable the checks, set a window
    that spans everything."""
    from datetime import date

    def _parse(key):
        v = str(options.get(key) or "").strip()
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None

    wf, wt = _parse("window_from"), _parse("window_to")
    if wf is None and wt is None:
        return                    # nothing edited — keep the detected window
    dmr.window_from = wf
    dmr.window_to = wt


def _run(run_id: str) -> None:
    run = db.run_get(run_id)
    if not run:
        return
    options = json.loads(run.get("options_json") or "{}")
    try:
        db.run_update(run_id, status="running", phase="parse",
                      message="Parsing workbooks…")
        plog = run_upload_task_sync(parse_plog, run["plog_path"])
        dmr = run_upload_task_sync(parse_dmr, run["dmr_path"])
        apply_window_override(dmr, options)

        perim = None
        perim_warning = None
        if run.get("perimeter_hash"):
            perim = perimeter_mod.load_cached(
                run["perimeter_hash"], filename=run.get("perimeter_name") or "")
            if perim is None:
                perim_warning = (
                    "The perimeter file recorded for this run is no longer in "
                    "the cache — running without the perimeter split.")

        def progress(phase: str, done: int, total: int, msg: str) -> None:
            db.run_progress(run_id, phase, done, total, msg)

        def tikhub_counter() -> None:
            db.run_bump_counter(run_id, "tikhub_calls")

        def llm_counter() -> None:
            db.run_bump_counter(run_id, "llm_calls")

        verdicts = run_pipeline(
            plog, dmr, progress=progress, tikhub_counter=tikhub_counter,
            retry_failed_links=bool(options.get("retry_failed_links")),
            perimeter=perim,
        )

        if options.get("use_llm", True):
            db.run_progress(run_id, "adjudicate", len(verdicts), len(verdicts),
                            "Adjudicating residue with Claude…")
            adjudicate(verdicts, llm_counter=llm_counter)

        counts = status_counts(verdicts)
        reverse_rows = reverse_audit(plog, dmr, verdicts)

        summary = {"zh": "", "en": ""}
        if options.get("use_llm", True):
            db.run_progress(run_id, "summary", len(verdicts), len(verdicts),
                            "Drafting run summary…")
            summary = summarize_run(verdicts, counts,
                                    plog.warnings + dmr.warnings,
                                    llm_counter=llm_counter)

        result = {
            "verdicts": [v.to_dict() for v in verdicts],
            "counts": counts,
            "buckets": summary_buckets(counts),
            # document-level context: DMR engagement numbers in the rows are
            # early-crawl snapshots, never a matching signal
            "engagement_caveat": ENGAGEMENT_CAVEAT,
            "perimeter_meta": ({
                "filename": perim.filename,
                "extraction_date": perim.extraction_date,
                "rows": len(perim.rows),
                "redbook_count": len(perim.by_redbook),
            } if perim else None),
            "perimeter_warning": perim_warning,
            "reverse_audit": reverse_rows,
            "plog_meta": {
                "sheet": plog.sheet, "header_row": plog.header_row,
                "rows": len(plog.rows), "campaigns": plog.campaigns,
                "date_range": [str(d) if d else None for d in plog.date_range],
                "warnings": plog.warnings,
            },
            "dmr_meta": {
                "sheet": dmr.sheet, "header_row": dmr.header_row,
                "rows": len(dmr.rows),
                "window": [str(dmr.window_from) if dmr.window_from else None,
                           str(dmr.window_to) if dmr.window_to else None],
                "warnings": dmr.warnings,
            },
        }
        db.run_update(
            run_id, status="done", phase="done",
            message="Run complete.",
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            summary_json=json.dumps(summary, ensure_ascii=False),
        )
    except Exception as e:
        db.run_update(run_id, status="error", phase="error",
                      message=f"Run failed: {e}",
                      error=traceback.format_exc()[:8000])
