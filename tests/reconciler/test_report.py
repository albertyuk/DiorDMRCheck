"""The annotated export must reproduce the reference layout: columns A–R
untouched, S = human vocabulary with no header, evidence in T+."""
from __future__ import annotations

from openpyxl import load_workbook

from app.reconciler.pipeline import run_pipeline
from app.reconciler.parsers import parse_dmr, parse_plog
from app.reconciler.export import write_annotated_xlsx
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
    # evidence headers start at T
    assert ann.cell(row=plog.header_row, column=20).value == "STATUS"

    by = {(v.campaign, v.no): v for v in verdicts}
    # matched row → blank S
    v = by[("PLOG #001", "1")]
    assert ann.cell(row=v.excel_row, column=19).value in (None, "")
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


def test_export_neutralizes_formula_capable_external_text(
        plog_path, dmr_path, fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    verdict = verdicts[0]
    verdict.matched_blogger = '=HYPERLINK("https://evil.invalid","click")'
    verdict.llm_rationale_en = "+SUM(1,1)"
    verdict.notes = ["@malicious"]
    overrides = {
        verdict.excel_row: {"status": "无帖子", "note": "-1+1"}
    }
    out = tmp_path / "safe.xlsx"
    write_annotated_xlsx(
        plog_path, str(out), verdicts, plog.header_row, plog.sheet, overrides
    )
    ws = load_workbook(out, data_only=False)[plog.sheet]
    for column in (23, 32, 34):  # W blogger, AF rationale, AH notes
        cell = ws.cell(verdict.excel_row, column)
        assert cell.data_type == "s"
        assert cell.value.startswith("'")


def test_audit_json_uses_effective_override_counts_and_retains_pipeline(
        plog_path, dmr_path, fake_resolver):
    import json
    from app.reconciler.export import build_audit_json
    from app.reconciler.pipeline import status_counts

    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    verdicts = run_pipeline(plog, dmr)
    verdict = next(v for v in verdicts if v.status == "NO_POST")
    overrides = {
        verdict.excel_row: {"status": "无博主", "note": "reviewed"}
    }
    doc = json.loads(build_audit_json(
        {"id": "t", "summary_json": None}, verdicts,
        status_counts(verdicts), {}, {}, [], overrides=overrides,
    ))
    effective = next(v for v in doc["verdicts"]
                     if v["excel_row"] == verdict.excel_row)
    assert effective["status"] == "NO_BLOGGER"
    assert effective["pipeline_status"] == "NO_POST"
    assert doc["counts"]["NO_BLOGGER"] >= 1
    assert doc["counts"].get("NO_POST", 0) == doc["pipeline_counts"]["NO_POST"] - 1
    assert doc["summary_basis"] == "pipeline_before_human_overrides"


def test_invalid_legacy_override_is_ignored_not_executed(
        plog_path, dmr_path, fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    verdicts = run_pipeline(plog, parse_dmr(dmr_path))
    verdict = verdicts[0]
    overrides = {
        verdict.excel_row: {"status": "=1+1", "note": "legacy"}
    }
    out = tmp_path / "legacy.xlsx"
    write_annotated_xlsx(
        plog_path, str(out), verdicts, plog.header_row, plog.sheet, overrides
    )
    ws = load_workbook(out, data_only=False)[plog.sheet]
    assert ws.cell(verdict.excel_row, 19).data_type != "f"
    assert ws.cell(verdict.excel_row, 19).value == (verdict.column_s() or None)


def test_xlsx_contains_hidden_perimeter_provenance(
        plog_path, dmr_path, fake_resolver, tmp_path):
    plog = parse_plog(plog_path)
    verdicts = run_pipeline(plog, parse_dmr(dmr_path))
    out = tmp_path / "provenance.xlsx"
    write_annotated_xlsx(
        plog_path, str(out), verdicts, plog.header_row, plog.sheet,
        perimeter_meta={"hash": "abc", "warnings": ["stale"]},
        perimeter_warning="cache unavailable",
    )
    wb = load_workbook(out)
    meta = wb["_DMR_AUDIT_META"]
    assert meta.sheet_state == "hidden"
    values = {row[0].value: row[1].value for row in meta.iter_rows(min_row=2)}
    assert values["hash"] == "abc"
    assert values["warning"] == "cache unavailable"


def test_perimeter_provenance_never_deletes_same_named_user_sheet(
        plog_path, dmr_path, fake_resolver, tmp_path):
    source = tmp_path / "collision.xlsx"
    wb = load_workbook(plog_path)
    user_sheet = wb.create_sheet("_DMR_AUDIT_META")
    user_sheet["A1"] = "USER CONTENT"
    wb.save(source)
    plog = parse_plog(str(source))
    out = tmp_path / "collision-out.xlsx"
    write_annotated_xlsx(
        str(source), str(out), run_pipeline(plog, parse_dmr(dmr_path)),
        plog.header_row, plog.sheet, perimeter_meta={"hash": "abc"},
    )
    exported = load_workbook(out)
    assert exported["_DMR_AUDIT_META"]["A1"].value == "USER CONTENT"
    assert exported["_DMR_AUDIT_META_2"].sheet_state == "hidden"
