"""Language toggle + translation machinery.

The contract under test: English is the identity rendering (byte-for-byte
what a non-localized app would emit), every t() key used anywhere resolves in
the ZH dict, and dynamic run-time diagnostics translate by pattern with their
row numbers / counts carried over.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, i18n
from app import main as main_mod

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "app" / "templates"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")   # open mode
    with TestClient(main_mod.app) as c:
        yield c


# ------------------------------------------------------------------ toggle

def test_toggle_sets_cookie_and_returns_to_referer(client):
    r = client.get("/lang/zh", headers={"referer": "http://host/efficiency"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/efficiency"
    assert "dmr_lang=zh" in r.headers["set-cookie"]


def test_toggle_keeps_query_string(client):
    r = client.get("/lang/zh", headers={"referer": "http://host/team?msg=hello"},
                   follow_redirects=False)
    assert r.headers["location"] == "/team?msg=hello"


def test_toggle_ignores_offsite_referer(client):
    r = client.get("/lang/zh", headers={"referer": "https://evil.example//x"},
                   follow_redirects=False)
    assert r.headers["location"] == "/"


def test_unknown_lang_falls_back_to_english(client):
    r = client.get("/lang/klingon", follow_redirects=False)
    assert "dmr_lang=en" in r.headers["set-cookie"]
    client.cookies.set("dmr_lang", "klingon")   # tampered cookie
    assert "Past runs" in client.get("/").text


def test_lang_route_reachable_without_login(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "secret-setup-code")
    with TestClient(main_mod.app) as c:
        r = c.get("/lang/zh", follow_redirects=False)
        assert r.status_code == 303          # not bounced to /login
        assert "dmr_lang=zh" in r.headers["set-cookie"]


def test_english_is_default_and_shows_toggle(client):
    body = client.get("/").text
    assert 'lang="en"' in body
    assert 'href="/lang/zh"' in body and "中文" in body
    assert "Past runs" in body


def test_zh_renders_chrome_and_reverse_toggle(client):
    client.cookies.set("dmr_lang", "zh")
    body = client.get("/").text
    assert 'lang="zh-CN"' in body
    assert "投放效率" in body and "小红书" in body
    assert 'href="/lang/en"' in body and "English" in body


# ---------------------------------------------------------------- coverage

_CALL = re.compile(r"""\bt\(\s*(?:"((?:[^"\\]|\\.)+)"|'((?:[^'\\]|\\.)+)')""")


def _template_keys() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for f in sorted(TEMPLATES.rglob("*.html")):
        for m in _CALL.finditer(f.read_text()):
            key = (m.group(1) or m.group(2)).replace('\\"', '"').replace("\\'", "'")
            used.setdefault(key, []).append(str(f.relative_to(TEMPLATES)))
    return used


def test_every_template_key_has_a_chinese_translation():
    used = _template_keys()
    assert used, f"no t() keys found under template root {TEMPLATES}"
    missing = {k: v for k, v in used.items() if k not in i18n.ZH}
    assert not missing, f"t() keys missing from i18n.ZH: {missing}"


def test_no_multiline_or_padded_keys():
    # a single LEADING space is legal — it absorbs the template's separator
    # so Chinese (which wants none before full-width punctuation) can drop it
    bad = [k for k in _template_keys()
           if "\n" in k or "  " in k or k != k.rstrip() or k.startswith("  ")]
    assert not bad, f"keys must be single-line, no trailing/double spaces: {bad}"


def test_placeholder_parity_between_english_and_chinese():
    ph = re.compile(r"\{[a-z_0-9]*\}")
    bad = {k: z for k, z in i18n.ZH.items()
           if sorted(ph.findall(k)) != sorted(ph.findall(z))}
    assert not bad, f"zh must keep the same placeholders as the key: {bad}"


# ------------------------------------------------------------- dynamic td()

def test_td_translates_known_runtime_messages():
    td = i18n.make_td("zh")
    assert td("Run complete.") == "核对完成。"
    assert td("Waiting for a free run slot…") == "正在排队，等待可用的核对名额…"
    assert td("Resolving links 37/101…") == "正在解析链接 37/101…"
    assert "核对失败：boom" == td("Run failed: boom")
    got = td("KOL row 17: POST DATE 'soon' could not be parsed — "
             "date-based checks are skipped for this row.")
    assert "17" in got and "'soon'" in got and "跳过" in got
    got = td("DMR row 9: Link hyperlink embeds PostID aaa but the PostID "
             "column says bbb — using the PostID column for the join.")
    assert "9" in got and "aaa" in got and "bbb" in got and "为准" in got


def test_td_translates_macro_perimeter_notes():
    """Every Macro-labeled perimeter note must translate like its Micro
    sibling (the strings are generated in pipeline._annotate_name_hits and
    apply_perimeter with a per-list label)."""
    td = i18n.make_td("zh")
    got = td("Blogger is inside DMR's monitored Macro perimeter "
             "(REDBOOK_ID 5fabc) yet absent from the export — a genuine "
             "DMR gap, grouped with 无帖子.")
    assert "Macro Perimeter" in got and "5fabc" in got and "漏抓" in got
    got = td("同名Macro Perimeter条目但REDBOOK_ID不同（近似未命中）/ same-name "
             "macro perimeter entry carries a different REDBOOK_ID "
             "(5faaa vs resolved 5fbbb)")
    assert got.startswith("同名Macro Perimeter条目") and "5faaa" in got
    assert "same-name" not in got
    got = td("3个同名Macro Perimeter条目，无法按名字判定 / name matches "
             "multiple macro perimeter rows — never auto-picked by name")
    assert got == "3个同名Macro Perimeter条目，无法按名字判定——绝不按名字自动选取。"
    got = td("在Macro Perimeter名单但未登记REDBOOK_ID — register the ID; DMR "
             "cannot crawl an unregistered account")
    assert "register" not in got and got.startswith("在Macro Perimeter名单")


def test_td_translates_efficiency_findings_and_insights():
    """The V2–V10 findings and the insight bullets are generated in English
    by effreport.py; the zh report page translates them at render time."""
    td = i18n.make_td("zh")
    got = td("3 row(s) missing metric values — excluded from all metrics "
             "(missing_row_policy=exclude_warn).")
    assert got.startswith("3 行缺少指标值")
    got = td("Duplicate POST LINK shared by 2 rows (鸡腿子 / nnuuxx): "
             "…abcdef123456789012345678 — likely a copy-paste error in the "
             "source; metrics keep both rows, but verify.")
    assert "鸡腿子 / nnuuxx" in got and "复制粘贴" in got
    got = td("KOC SOFT: top 2 post(s) hold 90% of group impressions — pooled "
             "CPM is dragged by outliers; plan on per-post ≈348, not pooled "
             "88. On-slide caveat added.")
    assert "KOC SOFT" in got and "≈348" in got and "88" in got and "爆" not in got
    got = td("CAUTION: KOC SOFT CARRIED BY 2 VIRAL POSTS (90% OF GROUP "
             "IMPRESSIONS) — PLAN ON ~¥348 CPM, NOT ¥88")
    assert "爆款" in got and "¥348" in got and "¥88" in got
    got = td("TOP SOFT = 1 POST(S) ONLY — NOT A BENCHMARK")
    assert "TOP SOFT" in got and "不可作为基准" in got
    got = td("Basis: pooled — group total spend ÷ total impressions "
             "(CPM, ¥ per 1,000) / total engagements (CPE). "
             "n = 44 posts: TOP 1 · MID 5 · BOT 30 · KOC 8")
    assert got.startswith("口径：合并") and "n = 44 篇" in got


def test_td_passes_unknown_text_and_english_through():
    assert i18n.make_td("zh")("some novel diagnostic") == "some novel diagnostic"
    assert i18n.make_td("en")("Run complete.") == "Run complete."


def test_handler_messages_translate(client):
    client.cookies.set("dmr_lang", "zh")
    r = client.post("/efficiency",
                    files={"report": ("junk.xlsx", b"not a zip",
                                      "application/zip")}, data={})
    assert r.status_code == 422
    assert "上传的文件无法按 .xlsx 读取" in r.text


# ------------------------------------------------- wording-drift detector

# English keys that intentionally share one Chinese value. Because the
# catalog is keyed by the exact emitted English text, wording drift at an
# emit site silently forces a near-duplicate catalog row (the plural/singular
# .xlsx pair was the first real case). Every duplicate group must be listed
# here deliberately — a new one fails this lint until the emit sites are
# unified or the group is consciously allowed.
ALLOWED_ZH_DUPLICATE_GROUPS = {
    frozenset({"KOL Efficiency Report", "KOL efficiency report"}),
    frozenset({"Could not read the uploaded file(s) as .xlsx: {e}",
               "Could not read the uploaded file as .xlsx: {e}"}),  # plural vs singular sites
    frozenset({"Human override", "override"}),
    frozenset({"Guide", "Quick guide"}),
    frozenset({"date the post went live", "Date"}),
    frozenset({"sheet", "Sheet"}),
    frozenset({"header row", "Header row"}),
    # flow-stepper labels share Chinese with same-meaning table headers /
    # buttons — different English contexts, one natural zh term
    frozenset({"Reconcile", "Run"}),
    frozenset({"Retry", "Retry run"}),
}


def test_zh_duplicate_values_are_deliberate():
    from collections import defaultdict
    groups = defaultdict(list)
    for en, zh in i18n.ZH.items():
        groups[zh].append(en)
    unexpected = [
        sorted(ens) for zh, ens in groups.items()
        if len(ens) > 1 and frozenset(ens) not in ALLOWED_ZH_DUPLICATE_GROUPS
    ]
    assert not unexpected, (
        "New English keys translating to identical Chinese — emit-site "
        "wording drift? Reuse the existing English string (share a constant) "
        f"or add the group to ALLOWED_ZH_DUPLICATE_GROUPS: {unexpected}")
