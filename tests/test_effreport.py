"""KOL efficiency report: engine (effreport) and deck generation.

The synthetic workbook is built so every metric is hand-computable, and each
validation rule V2–V10 has a row that trips it. The golden test against the
real client workbook runs only when the (gitignored) file is present.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.deck import assert_chart_cache, build_deck
from app.effreport import (ReportConfig, VerificationError, analyze,
                           build_insights, compute_metrics,
                           compute_metrics_pandas, verify_dual_path)

EFF_HEADERS = [
    "NO", "MCN", "CAMPAIGN", "TYPE", "LEVEL", "NAME", "FAN BASE（K)",
    "POST DATE", "MICRO MACRO", "POST LINK", "IMPRESSION", "LIKE",
    "COLLECTION", "COMMENT", "TTL  ENGAGEMENT", "PRICE", "CPM", "CPE",
]

PAID, SOFT = "报备图文", "软植图文"


def _row(no, type_, level, name, fan, link, impr, like, coll, comm, ttl,
         price, cpm=None):
    return [no, "", "EFF #001", type_, level, name, fan,
            datetime(2026, 6, 1 + (no - 1) % 28), "", link, impr, like, coll,
            comm, ttl,
            price, cpm, None]


def build_eff_bytes() -> bytes:
    """43 active rows. Hand-computed groups:

    MID PAID  n=2: prices 20k+10k, impr 200k+100k, eng 2000+1000
              → pooled CPM 100, per-post CPM 100, pooled CPE 10, avg 15k
    MID SOFT  n=3: 6k×3, impr 100k/50k/50k, eng 500/250/250
              → pooled CPM 90, per-post CPM 100, pooled CPE 18, avg 6k
    BOT SOFT  n=2: one normal + one IMPRESSION=0 (V3)
              → pooled CPM 50 (zero-impr row out of CPM only), CPE 10
    KOC PAID  n=4: impr 90k/90k/10k/10k → top-2 hold 90% (V10)
    TOP SOFT  n=1 (V9); BOT PAID n=30 (filler, makes TOP SOFT a <3% sliver)
    plus: V7 unknown TYPE row (in totals, no group), V2 missing-PRICE row
    (excluded), V6 duplicate link, V4 identity break, V5 CPM-column drift,
    V8 尾部+底部 both present.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER KOL LIST"
    ws.append(EFF_HEADERS)
    rows = [
        _row(1, PAID, "腰部达人", "mp1", 800, "http://x.co/mp1",
             200_000, 1_500, 300, 200, 2_000, 20_000,
             cpm=0.5),                                    # V5: true 0.1
        _row(2, PAID, "腰部达人", "mp2", 700, "http://x.co/mp2",
             100_000, 800, 100, 100, 1_000, 10_000),
        _row(3, SOFT, "腰部达人", "ms1", 600, "http://x.co/dup",
             100_000, 400, 50, 50, 500, 6_000),
        _row(4, SOFT, "腰部达人", "ms2", 600, "http://x.co/dup",  # V6 pair
             50_000, 200, 25, 25, 250, 6_000),
        _row(5, SOFT, "腰部达人", "ms3", 600, "http://x.co/ms3",
             50_000, 200, 25, 25, 250, 6_000),
        _row(6, SOFT, "尾部达人", "bs1", 250, "http://x.co/bs1",
             100_000, 400, 50, 50, 500, 5_000),
        _row(7, SOFT, "底部达人", "bs2", 150, "http://x.co/bs2",
             0, 400, 50, 50, 500, 5_000),                 # V3 zero impr
        _row(8, PAID, "KOC", "kp1", 50, "http://x.co/kp1",
             90_000, 500, 50, 50, 600, 1_000),
        _row(9, PAID, "KOC", "kp2", 50, "http://x.co/kp2",
             90_000, 500, 50, 50, 999, 1_000),            # V4: 600≠999
        _row(10, PAID, "KOC", "kp3", 50, "http://x.co/kp3",
             10_000, 100, 10, 10, 120, 1_000),
        _row(11, PAID, "KOC", "kp4", 50, "http://x.co/kp4",
             10_000, 100, 10, 10, 120, 1_000),
        _row(12, SOFT, "头部达人", "ts1", 1_500, "http://x.co/ts1",
             1_000_000, 5_000, 500, 500, 6_000, 90_000),  # V9 n=1
        _row(13, "其他合作", "腰部达人", "unk", 500, "http://x.co/unk",
             10_000, 50, 5, 5, 60, 2_000),                # V7 TYPE
        _row(14, PAID, "腰部达人", "gap", 500, "http://x.co/gap",
             10_000, 50, 5, 5, 60, None),                 # V2 no PRICE
    ]
    for i in range(30):                                   # BOT PAID filler
        rows.append(_row(15 + i, PAID, "尾部达人", f"bp{i}", 250,
                         f"http://x.co/bp{i}", 10_000, 80, 10, 10, 100,
                         1_000))
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def analysis() -> dict:
    return analyze(io.BytesIO(build_eff_bytes()), ReportConfig())


def _codes(analysis):
    return {f["code"] for f in analysis["findings"]}


# ------------------------------------------------------------------ metrics

def test_group_metrics_hand_computed(analysis):
    g = analysis["metrics"]["groups"]
    mp = g["MID PAID"]
    assert mp["n"] == 2
    assert mp["avg_price"] == pytest.approx(15_000)
    assert mp["cpm_pooled"] == pytest.approx(100.0)
    assert mp["cpm_perpost"] == pytest.approx(100.0)
    assert mp["cpe_pooled"] == pytest.approx(10.0)
    ms = g["MID SOFT"]
    assert ms["cpm_pooled"] == pytest.approx(90.0)
    assert ms["cpm_perpost"] == pytest.approx(100.0)
    assert ms["cpe_pooled"] == pytest.approx(18.0)


def test_zero_impression_row_excluded_from_cpm_only(analysis):
    bs = analysis["metrics"]["groups"]["BOT SOFT"]
    assert bs["n"] == 2                       # still counted in share/n
    assert bs["cpm_pooled"] == pytest.approx(50.0)   # 5000/100000*1000
    assert bs["cpe_pooled"] == pytest.approx(10.0)   # both rows have eng
    assert "V3" in _codes(analysis)


def test_missing_group_is_absent_not_zero(analysis):
    assert "TOP PAID" not in analysis["metrics"]["groups"]


def test_unclassified_counted_in_totals_not_groups(analysis):
    t = analysis["metrics"]["totals"]
    assert t["unclassified"] == 1             # the V7 TYPE row
    assert t["rows"] == 43                    # 44 parsed - 1 excluded (V2)
    n_sum = sum(g["n"] for g in analysis["metrics"]["groups"].values())
    assert n_sum + t["unclassified"] == t["rows"]


def test_validation_findings_fire(analysis):
    assert {"V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9",
            "V10"} <= _codes(analysis)
    v10 = [f for f in analysis["findings"] if f["code"] == "V10"]
    assert any(f["message"].startswith("KOC PAID") for f in v10)
    # trivially-concentrated small groups are V9's job, not V10's
    assert not any(f["message"].startswith("TOP SOFT") for f in v10)


def test_dual_path_and_reconciliation_pass(analysis):
    assert not analysis["blocked"]            # WARNs never block


def test_dual_path_catches_divergence():
    rows_bytes = build_eff_bytes()
    from app.effreport import classify, parse_report, validate
    cfg = ReportConfig()
    rows, findings, _ = parse_report(io.BytesIO(rows_bytes))
    classify(rows, cfg, findings)
    validate(rows, cfg, findings)
    primary = compute_metrics(rows, cfg)
    secondary = compute_metrics_pandas(rows, cfg)
    secondary["MID PAID"]["cpm_pooled"] += 1.0
    with pytest.raises(VerificationError):
        verify_dual_path(primary, secondary)


def test_block_policy_marks_blocked():
    cfg = ReportConfig(missing_row_policy="block")
    a = analyze(io.BytesIO(build_eff_bytes()), cfg)
    assert a["blocked"]
    assert any(f["severity"] == "ERROR" for f in a["findings"])


def test_fanbase_tier_mode():
    a = analyze(io.BytesIO(build_eff_bytes()), ReportConfig(tier_mode="fanbase"))
    g = a["metrics"]["groups"]
    # ts1 has 1500K fans → TOP either way; bs1 (250K) lands in BOT,
    # bs2 (150K) drops to KOC under thresholds
    assert g["TOP SOFT"]["n"] == 1
    assert g["KOC SOFT"]["n"] == 1


def test_v1_missing_columns_raises():
    wb = Workbook()
    ws = wb.active
    ws.append(["NAME", "POST LINK"])          # header found, columns missing
    ws.append(["x", "http://x.co/1"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with pytest.raises(ValueError, match="V1"):
        analyze(buf)


# ----------------------------------------------------------------- insights

def test_insights_are_data_driven_and_never_cross_wave(analysis):
    ins = analysis["insights"]
    joined = " ".join(ins["price"] + ins["efficiency"] + ins["caveats"]
                      + [ins["footnote"]])
    # the hard rule: no fabricated wave-over-wave comparisons
    for banned in ("WAVE", "PREVIOUS", "LAST ROUND", "VS. PRIOR", "环比", "上一波"):
        assert banned not in joined.upper()
    assert any("MID" in b for b in ins["price"])       # premium exists (24k>6k… wait 15k avg)
    assert ins["footnote"].startswith("Basis: pooled")


def test_caveats_cover_v9_and_v10(analysis):
    caveats = " ".join(analysis["insights"]["caveats"])
    assert "TOP SOFT = 1 POST(S) ONLY" in caveats
    assert "KOC PAID" in caveats and "VIRAL" in caveats


def test_per_post_basis_footnote():
    a = analyze(io.BytesIO(build_eff_bytes()), ReportConfig(basis="per_post"))
    assert a["insights"]["footnote"].startswith("Basis: per-post")


# --------------------------------------------------------------------- deck

def test_build_deck_and_chart_cache(analysis):
    pptx = build_deck(analysis)
    assert_chart_cache(pptx, analysis)        # must not raise
    with zipfile.ZipFile(io.BytesIO(pptx)) as z:
        charts = [n for n in z.namelist()
                  if n.startswith("ppt/charts/chart") and n.endswith(".xml")]
        assert len(charts) == 4               # donut, price, CPM, CPE
        donut = min(charts)
        xml = z.read(donut).decode()
    assert 'val="58"' in xml                  # hole size
    assert "showLeaderLines" in xml
    assert 'formatCode=\'0.0"%"\'' in xml or 'formatCode="0.0&quot;%&quot;"' in xml \
        or '0.0"%"' in xml                    # share number format present


def test_chart_cache_catches_tampering(analysis):
    pptx = build_deck(analysis)
    import copy
    tampered = copy.deepcopy(analysis)
    tampered["metrics"]["groups"]["MID PAID"]["avg_price"] += 1
    with pytest.raises(VerificationError):
        assert_chart_cache(pptx, tampered)


def test_deck_zh_language():
    a = analyze(io.BytesIO(build_eff_bytes()), ReportConfig(language="zh"))
    pptx = build_deck(a)
    assert_chart_cache(pptx, a)


# ----------------------------------------------------- golden (real file)

REAL = Path(__file__).resolve().parent.parent / "data" / "real" / "PLOG_DMR_CHECK.xlsx"


@pytest.mark.skipif(not REAL.exists(), reason="real client workbook not present")
def test_golden_real_workbook():
    a = analyze(str(REAL), ReportConfig())
    t = a["metrics"]["totals"]
    assert t["spend"] == pytest.approx(1_049_345)
    assert t["impressions"] == 17_820_424
    assert t["engagements"] == 216_300
    g = a["metrics"]["groups"]
    assert set(g) == {"TOP SOFT", "MID PAID", "MID SOFT", "BOT PAID",
                      "BOT SOFT", "KOC PAID", "KOC SOFT"}
    rounded_cpm = {k: round(v["cpm_pooled"]) for k, v in g.items()}
    assert rounded_cpm == {"TOP SOFT": 82, "MID PAID": 63, "MID SOFT": 103,
                           "BOT PAID": 48, "BOT SOFT": 117, "KOC PAID": 14,
                           "KOC SOFT": 88}
    rounded_cpe = {k: round(v["cpe_pooled"], 1) for k, v in g.items()}
    assert rounded_cpe == {"TOP SOFT": 2.8, "MID PAID": 6.7, "MID SOFT": 5.1,
                           "BOT PAID": 3.9, "BOT SOFT": 8.6, "KOC PAID": 1.5,
                           "KOC SOFT": 5.8}
    pptx = build_deck(a)
    assert_chart_cache(pptx, a)
