"""The annotated export must reproduce the reference layout: columns A–R
untouched, S = human vocabulary with no header, evidence in T+."""
from __future__ import annotations

from openpyxl import load_workbook

from app.reconciler.pipeline import run_pipeline
from app.reconciler.parsers import parse_dmr, parse_plog
from app.reconciler.export import EVIDENCE_HEADERS, write_annotated_xlsx
from tests import fixtures


def test_annotated_export(plog_path, dmr_path, fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    out = tmp_path / "annotated.xlsx"
    write_annotated_xlsx(plog_path, str(out), verdicts,
                         header_row=plog.header_row)

    orig = load_workbook(plog_path)["MASTER KOL LIST"]
    ann = load_workbook(str(out))["MASTER KOL LIST"]

    # Columns A–R byte-identical values
    for row in orig.iter_rows(min_col=1, max_col=18):
        for cell in row:
            assert ann.cell(row=cell.row, column=cell.column).value == cell.value

    # S has no header (the reference leaves S1 blank)
    assert ann.cell(row=plog.header_row, column=19).value in (None, "")
    # trimmed evidence block starts at T: status, matched blogger, then the
    # weighted-engagement-data section copied from the DMR file
    assert [ann.cell(row=plog.header_row, column=c).value
            for c in range(20, 20 + len(EVIDENCE_HEADERS))] == EVIDENCE_HEADERS
    # nothing is written beyond the evidence block
    assert ann.cell(row=plog.header_row,
                    column=20 + len(EVIDENCE_HEADERS)).value in (None, "")

    by = {(v.campaign, v.no): v for v in verdicts}
    # matched row → blank S + DMR engagement snapshot pulled verbatim
    v = by[("PLOG #001", "1")]
    assert ann.cell(row=v.excel_row, column=19).value in (None, "")
    assert ann.cell(row=v.excel_row, column=21).value == "墨池墨吟"
    assert ann.cell(row=v.excel_row, column=22).value == 14   # Likes_Retweet
    assert ann.cell(row=v.excel_row, column=23).value == 1    # Share_Favorites
    assert ann.cell(row=v.excel_row, column=24).value == 0    # Comments
    assert ann.cell(row=v.excel_row, column=25).value == 15   # Engagement
    assert ann.cell(row=v.excel_row, column=26).value == 14.5  # WEIGHTED ENG.
    # unmatched row → engagement section stays empty
    nv = by[("PLOG #002", "2")]
    assert all(ann.cell(row=nv.excel_row, column=c).value is None
               for c in range(21, 20 + len(EVIDENCE_HEADERS)))
    # mislabel row → the human's exact vocabulary
    v = by[("PLOG #001", "3")]
    assert ann.cell(row=v.excel_row, column=19).value == "有 但是DMR博主名字标注错误"
    # no-post row → 无帖子
    v = by[("PLOG #002", "1")]
    assert ann.cell(row=v.excel_row, column=19).value == "无帖子"
    # dead link row → Check链接错误 + candidate note
    v = by[("PLOG #001", "4")]
    s = ann.cell(row=v.excel_row, column=19).value
    assert s.startswith("Check链接错误") and fixtures.N_JITUI in s


def test_override_wins_in_export(plog_path, dmr_path, fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    out = tmp_path / "annotated.xlsx"
    v = next(x for x in verdicts if (x.campaign, x.no) == ("PLOG #002", "1"))
    overrides = {v.excel_row: {"status": "无博主", "note": "human says so"}}
    write_annotated_xlsx(plog_path, str(out), verdicts,
                         header_row=plog.header_row, sheet_name=plog.sheet,
                         overrides=overrides)
    ann = load_workbook(str(out))["MASTER KOL LIST"]
    assert ann.cell(row=v.excel_row, column=19).value == "无博主"


def test_match_blank_override_forces_empty_s(plog_path, dmr_path, fake_resolver,
                                             tmp_path):
    from app.reconciler.export import OVERRIDE_MATCH_BLANK
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    out = tmp_path / "annotated.xlsx"
    # the mislabel row would normally carry 有 但是DMR博主名字标注错误
    v = next(x for x in verdicts if (x.campaign, x.no) == ("PLOG #001", "3"))
    overrides = {v.excel_row: {"status": OVERRIDE_MATCH_BLANK, "note": ""}}
    write_annotated_xlsx(plog_path, str(out), verdicts,
                         header_row=plog.header_row, sheet_name=plog.sheet,
                         overrides=overrides)
    ann = load_workbook(str(out))["MASTER KOL LIST"]
    assert ann.cell(row=v.excel_row, column=19).value in (None, "")


def test_load_verdicts_tolerates_legacy_and_derived_keys():
    """Stored result documents carry derived fields (column_s) and may
    predate the schema (per-row engagement_caveat, unknown future keys) —
    rehydration must drop them, never TypeError on historical runs."""
    import json
    from app.reconciler.export import load_verdicts
    legacy_verdict = {
        "campaign": "C", "no": "1", "name": "n", "post_date": None,
        "post_link": "http://x", "excel_row": 2, "status": "MATCH",
        "column_s": "",                                # derived
        "engagement_caveat": "old per-row caveat",     # pre-schema
        "some_future_field": 123,                      # forward compat
        "candidates": [{
            "dmr_row": 5, "post_id": "p", "blogger": "b", "username": "u",
            "post_date": None, "date_delta_days": None,
            "likes_retweet": None, "name_method": "cjk-substring",
            "novel_candidate_key": True,               # forward compat
        }],
    }
    run = {"result_json": json.dumps({"verdicts": [legacy_verdict]})}
    (v,) = load_verdicts(run)
    assert v.status == "MATCH" and v.candidates[0].post_id == "p"


def test_audit_json_has_document_level_caveat(plog_path, dmr_path,
                                              fake_resolver):
    import json
    from app.reconciler.domain import ENGAGEMENT_CAVEAT
    from app.reconciler.export import build_audit_json
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    doc = json.loads(build_audit_json(
        {"id": "t", "summary_json": None}, verdicts, {}, {}, {}, []))
    assert doc["engagement_caveat"] == ENGAGEMENT_CAVEAT
    assert all("engagement_caveat" not in v for v in doc["verdicts"])


# ------------------------------------------- never overwrite populated cells

def _prefilled_copy(plog_path, tmp_path, s_edits=None, col_edits=None):
    """Copy the fixture PLOG and pre-populate cells like earlier human work."""
    import shutil
    p = tmp_path / "prefilled.xlsx"
    shutil.copy(plog_path, p)
    wb = load_workbook(str(p))
    ws = wb["MASTER KOL LIST"]
    for row, value in (s_edits or {}).items():
        ws.cell(row=row, column=19, value=value)
    for (row, col), value in (col_edits or {}).items():
        ws.cell(row=row, column=col, value=value)
    wb.save(str(p))
    return str(p)


def test_prefilled_s_cells_are_never_overwritten(plog_path, dmr_path,
                                                 fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    by = {(v.campaign, v.no): v for v in verdicts}
    matched_row = by[("PLOG #001", "1")].excel_row      # pipeline wants blank
    nopost_row = by[("PLOG #002", "1")].excel_row       # pipeline wants 无帖子

    src = _prefilled_copy(plog_path, tmp_path, s_edits={
        matched_row: "人工已确认OK",
        nopost_row: "无帖子",                            # human agrees
    })
    out = tmp_path / "ann.xlsx"
    write_annotated_xlsx(src, str(out), verdicts, header_row=plog.header_row)
    ann = load_workbook(str(out))["MASTER KOL LIST"]

    assert ann.cell(row=matched_row, column=19).value == "人工已确认OK"
    assert ann.cell(row=nopost_row, column=19).value == "无帖子"
    # disagreement is recorded in the STATUS cell (the trimmed layout has no
    # Notes column); agreement gets the plain "kept" marker
    status = ann.cell(row=matched_row, column=20).value or ""
    assert "S kept from source" in status and "pipeline verdict" in status
    agree_status = ann.cell(row=nopost_row, column=20).value or ""
    assert "S kept from source" in agree_status
    assert "pipeline verdict" not in agree_status
    # untouched rows still get the pipeline verdict as usual
    mislabel_row = by[("PLOG #001", "3")].excel_row
    assert ann.cell(row=mislabel_row, column=19).value == "有 但是DMR博主名字标注错误"


def test_ui_override_still_wins_over_prefilled_s(plog_path, dmr_path,
                                                 fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    verdicts = run_pipeline(plog, parse_dmr(dmr_path))
    by = {(v.campaign, v.no): v for v in verdicts}
    row = by[("PLOG #002", "1")].excel_row
    src = _prefilled_copy(plog_path, tmp_path, s_edits={row: "旧的人工备注"})
    out = tmp_path / "ann.xlsx"
    write_annotated_xlsx(src, str(out), verdicts, header_row=plog.header_row,
                         overrides={row: {"status": "无博主", "note": ""}})
    ann = load_workbook(str(out))["MASTER KOL LIST"]
    assert ann.cell(row=row, column=19).value == "无博主"   # explicit action wins


def test_evidence_block_shifts_past_populated_columns(plog_path, dmr_path,
                                                      fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    verdicts = run_pipeline(plog, parse_dmr(dmr_path))
    some_row = verdicts[0].excel_row
    # column T (20) already used by the human for their own notes
    src = _prefilled_copy(plog_path, tmp_path,
                          col_edits={(some_row, 20): "我的备注，别动"})
    out = tmp_path / "ann.xlsx"
    write_annotated_xlsx(src, str(out), verdicts, header_row=plog.header_row)
    ann = load_workbook(str(out))["MASTER KOL LIST"]
    assert ann.cell(row=some_row, column=20).value == "我的备注，别动"
    assert ann.cell(row=plog.header_row, column=20).value in (None, "")
    assert ann.cell(row=plog.header_row, column=21).value == "STATUS"
