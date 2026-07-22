"""Exports.

1. Annotated .xlsx — the original PLOG workbook byte-identical in layout
   (columns A–R untouched, values and formats preserved), column S carrying
   the human vocabulary with no header (matching PLOG_DMR_CHECK_1), and
   richer evidence in columns T+ (which the reference leaves free).
2. JSON audit log of the full run.
"""
from __future__ import annotations

import json
from typing import Optional

from openpyxl import load_workbook
from openpyxl.styles import Font

from ..core.xlsx import find_header_row
from .parsers import PLOG_REQUIRED
from dataclasses import fields as dataclass_fields

from .domain import ENGAGEMENT_CAVEAT, Candidate, Verdict

S_COL = 19  # column S
# The reference uses "已匹配/blank" for matched rows; this sentinel lets a human
# override force a blank S cell (asserting MATCH) rather than clearing the
# override.
OVERRIDE_MATCH_BLANK = "已匹配（清空S）"
EVIDENCE_HEADERS = [
    ("T", "STATUS"),
    ("U", "TIER"),
    ("V", "MATCHED DMR POSTID"),
    ("W", "MATCHED DMR BLOGGER"),
    ("X", "RESOLVED NOTE ID"),
    ("Y", "RESOLVED AUTHOR ID"),
    ("Z", "NAME METHOD"),
    ("AA", "DATE Δ (days)"),
    ("AB", "PLOG LIKE"),
    ("AC", "DMR LIKES (early snapshot — NOT comparable)"),
    ("AD", "CANDIDATES"),
    ("AE", "CLAUDE VERDICT"),
    ("AF", "CLAUDE RATIONALE"),
    ("AG", "PERIMETER"),
    ("AH", "NOTES"),
]
EVIDENCE_START_COL = 20  # column T


def _perimeter_text(v: Verdict) -> str:
    parts = []
    if v.perimeter_method:
        entry = f"{v.perimeter_method}: {v.perimeter_name or v.perimeter_namebis}"
        if v.perimeter_namebis and v.perimeter_name:
            entry += f" / {v.perimeter_namebis}"
        if v.perimeter_dmrid:
            entry += f" · DMRID {v.perimeter_dmrid}"
        if v.perimeter_redbook_id:
            entry += f" · REDBOOK {v.perimeter_redbook_id}"
        if v.perimeter_followers is not None:
            entry += f" · {v.perimeter_followers:,} followers"
        parts.append(entry)
    if v.perimeter_note:
        parts.append(v.perimeter_note)
    if v.perimeter_candidates:
        parts.append("candidates: " + " ; ".join(v.perimeter_candidates))
    if parts and v.perimeter_extraction_date:
        parts.append(f"extracted {v.perimeter_extraction_date}")
    return " | ".join(parts)


def _candidates_text(v: Verdict) -> str:
    parts = []
    for c in v.candidates[:5]:
        delta = f"Δ{c.date_delta_days:+d}d" if c.date_delta_days is not None else "Δ?"
        parts.append(f"{c.blogger} [{c.post_id}] {c.post_date or '?'} {delta} ({c.name_method})")
    return " ; ".join(parts)


def write_annotated_xlsx(plog_path: str, out_path: str, verdicts: list[Verdict],
                         header_row: int, sheet_name: Optional[str] = None,
                         overrides: Optional[dict] = None) -> None:
    """Copy the PLOG workbook and add column S (+ evidence columns).

    The workbook is loaded without data_only so formulas and formats in A–R
    survive untouched; we only ever write to columns >= S. The target sheet is
    the one parse_plog actually read (passed by name) — re-detection on the
    formula view could pick a different sheet.

    Pre-existing content is never overwritten: an S cell that already holds a
    value in the source keeps it (a UI override — an explicit action in this
    tool — still wins; the pipeline verdict stays visible in the evidence
    status column, with a note when it disagrees), and the evidence block
    shifts right past any column that already contains data.
    """
    wb = load_workbook(plog_path)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else None
    if ws is None:
        for candidate in wb.worksheets:
            if find_header_row(candidate, PLOG_REQUIRED):
                ws = candidate
                break
    if ws is None:
        ws = wb.active

    overrides = overrides or {}
    verdict_rows = [v.excel_row for v in verdicts]
    check_rows = [header_row] + verdict_rows

    def _populated(col: int) -> bool:
        return any(ws.cell(row=r, column=col).value not in (None, "")
                   for r in check_rows)

    # first contiguous fully-empty block wide enough for the evidence columns
    ev_start = EVIDENCE_START_COL
    width = len(EVIDENCE_HEADERS)
    while any(_populated(c) for c in range(ev_start, ev_start + width)):
        ev_start += 1

    bold = Font(bold=True)
    for col_idx, (_, title) in enumerate(EVIDENCE_HEADERS, start=ev_start):
        cell = ws.cell(row=header_row, column=col_idx, value=title)
        cell.font = bold
    # Column S intentionally has no header — the reference file leaves S1 blank.

    for v in verdicts:
        r = v.excel_row
        ov = overrides.get(r)
        existing_s = ws.cell(row=r, column=S_COL).value
        preserved = None
        if ov:
            s_text = "" if ov["status"] == OVERRIDE_MATCH_BLANK else ov["status"]
        elif existing_s not in (None, ""):
            preserved = str(existing_s)
            s_text = preserved            # keep the human's cell verbatim
        else:
            s_text = v.column_s()
        status = f"{v.status}{' (override)' if ov else ''}"
        rationale = " / ".join(x for x in (v.llm_rationale_zh, v.llm_rationale_en) if x)
        llm = (f"{v.llm_verdict} ({v.llm_confidence:.0%})"
               if v.llm_verdict and v.llm_confidence is not None else v.llm_verdict)
        notes = list(v.notes)
        if preserved is not None:
            pipeline_s = v.column_s()
            if preserved.strip() != (pipeline_s or "").strip():
                notes.append(
                    f"S already contained {preserved!r} — kept; pipeline "
                    f"verdict was {pipeline_s or '(blank=matched)'}")
            status += " (S kept from source)"
        ws.cell(row=r, column=S_COL, value=s_text or None)
        values = [
            status,
            v.tier,
            v.matched_post_id or None,
            v.matched_blogger or None,
            v.resolved_note_id or None,
            v.resolved_author_id or None,
            v.name_method or None,
            v.date_delta_days,
            v.plog_like,
            v.dmr_likes_retweet,
            _candidates_text(v) or None,
            llm or None,
            rationale or None,
            _perimeter_text(v) or None,
            " | ".join(notes + ([ov["note"]] if ov and ov.get("note") else [])) or None,
        ]
        for col_idx, value in enumerate(values, start=ev_start):
            ws.cell(row=r, column=col_idx, value=value)

    wb.save(out_path)


def build_audit_json(run: dict, verdicts: list[Verdict], counts: dict,
                     plog_meta: dict, dmr_meta: dict,
                     reverse_rows: list[dict],
                     overrides: Optional[dict] = None) -> str:
    overrides = overrides or {}
    doc = {
        "run_id": run.get("id"),
        "created_at": run.get("created_at"),
        "files": {"plog": run.get("plog_name"), "dmr": run.get("dmr_name")},
        "plog": plog_meta,
        "dmr": dmr_meta,
        "counts": counts,
        "engagement_caveat": ENGAGEMENT_CAVEAT,
        "tikhub_calls": run.get("tikhub_calls"),
        "llm_calls": run.get("llm_calls"),
        "summary": json.loads(run["summary_json"]) if run.get("summary_json") else None,
        "verdicts": [
            {**v.to_dict(),
             "override": overrides.get(v.excel_row)}
            for v in verdicts
        ],
        "reverse_audit": reverse_rows,
    }
    return json.dumps(doc, ensure_ascii=False, indent=2, default=str)


_VERDICT_FIELDS = frozenset(f.name for f in dataclass_fields(Verdict))
_CANDIDATE_FIELDS = frozenset(f.name for f in dataclass_fields(Candidate))


def load_verdicts(run: dict) -> list[Verdict]:
    """Rehydrate Verdict objects from a finished run's result_json.

    Unknown keys are dropped, not fatal: stored documents carry derived
    fields (column_s) and may predate the current schema (older runs stored
    a per-row engagement_caveat) — a rendering-side change must never make
    historical runs unexportable."""
    result = json.loads(run.get("result_json") or "{}")
    out = []
    for d in result.get("verdicts", []):
        d = dict(d)
        cands = [Candidate(**{k: v for k, v in c.items()
                              if k in _CANDIDATE_FIELDS})
                 for c in d.pop("candidates", [])]
        v = Verdict(**{k: v for k, v in d.items() if k in _VERDICT_FIELDS})
        v.candidates = cands
        out.append(v)
    return out
