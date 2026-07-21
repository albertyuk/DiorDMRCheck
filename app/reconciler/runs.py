"""Background run orchestration.

Each run executes in a daemon thread; progress and results are persisted in
SQLite so the web layer only ever polls the database. Partial failure is
normal — a dead link, a TikHub 4xx, a rate limit each become per-row status,
and the run completes. Only file-level parse failures abort a run.
"""
from __future__ import annotations

import json
import threading
import traceback

from ..core import db
from . import perimeter as perimeter_mod
from .adjudicator import adjudicate, summarize_run
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


def start_run(run_id: str) -> None:
    t = threading.Thread(target=_run, args=(run_id,), daemon=True)
    t.start()


def _run(run_id: str) -> None:
    run = db.run_get(run_id)
    if not run:
        return
    options = json.loads(run.get("options_json") or "{}")
    try:
        db.run_update(run_id, status="running", phase="parse",
                      message="Parsing workbooks…")
        plog = parse_plog(run["plog_path"])
        dmr = parse_dmr(run["dmr_path"])

        perim = None
        perim_warning = None
        if run.get("perimeter_hash"):
            perim = perimeter_mod.load_cached(run["perimeter_hash"])
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
