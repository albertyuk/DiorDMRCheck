"""LLM header mapping + human audit.

The LLM call is mocked throughout — tests pin the contract around it: the
proposal is validated, NOTHING applies without approval, applying rewrites
only header cells, approved mappings are cached by header signature, and the
efficiency flow never touches disk.
"""
from __future__ import annotations

import io
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import config
from app.remap import mapper as schema_map
from app import main as main_mod
from app.remap import service as remap_service
from app.core.textnorm import header_key

# ------------------------------------------------------------------ fixtures

CN_PLOG_HEADERS = ["序号", "机构", "项目", "形式", "级别", "博主昵称",
                   "粉丝(千)", "发布日期", "备注", "笔记链接", "曝光量",
                   "点赞", "收藏", "评论", "互动总量", "价格"]


def build_cn_plog_bytes() -> bytes:
    """A PLOG-equivalent tracker with Chinese headers — the fingerprint
    (NAME + POST LINK) cannot bind."""
    wb = Workbook()
    ws = wb.active
    ws.title = "达人列表"
    ws.append(["内部使用 KOL 投放追踪表"])          # metadata row above header
    ws.append(CN_PLOG_HEADERS)
    for no in (1, 2, 3):
        ws.append([no, "MCN", "WAVE #1", "报备图文", "KOC", f"博主{no}",
                   88, datetime(2026, 6, no), "", f"http://xhslink.com/cn{no}",
                   10000 * no, 500, 40, 10, 550, 2000])
    link_cell = ws.cell(row=3, column=10)           # hyperlink must survive
    link_cell.hyperlink = "http://xhslink.com/cn1"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CN_PLOG_PROPOSAL = {
    "sheet": "达人列表", "header_row": 2,
    "columns": {"no": 1, "campaign": 3, "name": 6, "postdate": 8,
                "postlink": 10, "impression": 11, "like": 12,
                "collection": 13, "comment": 14, "ttlengagement": 15},
    "confidence": {"name": 0.97, "postlink": 0.95, "postdate": 0.9,
                   "impression": 0.7},
    "warnings": ["粉丝(千) appears to be follower count in thousands"],
}


def _clear_mapping_cache():
    from app.core import db
    with db.connect() as conn:
        conn.execute("DELETE FROM settings WHERE key LIKE 'schemamap:%'")
        conn.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        schema_map, "_call_llm",
        lambda system, user: json.dumps(CN_PLOG_PROPOSAL, ensure_ascii=False))
    remap_service.PENDING_MAPS.clear()
    _clear_mapping_cache()      # tests share one SQLite settings table
    with TestClient(main_mod.app) as c:
        yield c
    remap_service.PENDING_MAPS.clear()
    _clear_mapping_cache()


def _upload_run(client, plog_bytes):
    from tests import fixtures
    import tempfile, os
    fd, dmr_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    fixtures.build_dmr(dmr_path)
    dmr_bytes = open(dmr_path, "rb").read()
    os.unlink(dmr_path)
    mime = ("application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet")
    return client.post("/upload", files={
        "plog": ("cn_tracker.xlsx", plog_bytes, mime),
        "dmr": ("dmr.xlsx", dmr_bytes, mime),
    }, follow_redirects=False)


# ----------------------------------------------------------------- invariants

def test_canonical_headers_roundtrip_through_header_key():
    """apply_mapping writes the canonical text; the parsers re-derive keys
    from it — every canonical text must normalize to exactly its key."""
    for kind, fields in schema_map.FIELDS.items():
        for text, key, _req, _desc in fields:
            assert header_key(text) == key, (kind, text, key)


def test_field_descriptions_have_chinese_translations():
    from app.i18n import ZH
    for fields in schema_map.FIELDS.values():
        for _text, _key, _req, desc in fields:
            assert desc in ZH, f"audit-page description missing zh: {desc!r}"


def test_apply_mapping_touches_only_header_cells():
    data = build_cn_plog_bytes()
    out = schema_map.apply_mapping(
        data, "plog", "达人列表", 2,
        {k: v for k, v in CN_PLOG_PROPOSAL["columns"].items()})
    wb = load_workbook(io.BytesIO(out))
    ws = wb["达人列表"]
    assert ws.cell(row=2, column=6).value == "NAME"
    assert ws.cell(row=2, column=10).value == "POST LINK"
    assert ws.cell(row=2, column=2).value == "机构"        # unmapped: untouched
    assert ws.cell(row=1, column=1).value == "内部使用 KOL 投放追踪表"
    assert ws.cell(row=3, column=6).value == "博主1"        # data untouched
    assert ws.cell(row=3, column=10).hyperlink is not None  # hyperlink survives
    from app.reconciler.parsers import parse_plog
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    open(p, "wb").write(out)
    parsed = parse_plog(p)
    os.unlink(p)
    assert len(parsed.rows) == 3 and parsed.rows[0].name == "博主1"


def test_apply_mapping_decollides_duplicate_canonical_headers():
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["NAME", "真实昵称", "链接"])   # a stray literal NAME column
    ws.append(["wrong", "right", "http://x"])
    buf = io.BytesIO()
    wb.save(buf)
    out = schema_map.apply_mapping(buf.getvalue(), "plog", "S", 1,
                                   {"name": 2, "postlink": 3})
    ws2 = load_workbook(io.BytesIO(out))["S"]
    assert ws2.cell(row=1, column=2).value == "NAME"
    assert ws2.cell(row=1, column=1).value == "(original) NAME"


def test_propose_drops_out_of_range_and_duplicate_columns(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    bad = dict(CN_PLOG_PROPOSAL,
               columns={"name": 6, "postlink": 6, "impression": 999})
    monkeypatch.setattr(schema_map, "_call_llm",
                        lambda s, u: json.dumps(bad, ensure_ascii=False))
    sample = schema_map.build_sample(build_cn_plog_bytes())
    prop = schema_map.propose(sample, "plog")
    assert prop.columns["name"] == 6
    assert prop.columns["postlink"] is None      # duplicate dropped
    assert prop.columns["impression"] is None    # out of range dropped


# ----------------------------------------------------------------- run flow

def test_run_flow_audit_then_approve(client):
    r = _upload_run(client, build_cn_plog_bytes())
    assert r.status_code == 303 and r.headers["location"].startswith("/remap/")
    token = r.headers["location"].rsplit("/", 1)[1]

    page = client.get(f"/remap/{token}")
    assert page.status_code == 200
    body = page.text
    assert "博主昵称" in body                      # original header shown
    assert "博主1" in body                         # sample values shown
    assert "follower count in thousands" in body   # model warning surfaced
    assert "Nothing runs until you approve." in body
    assert "97%" in body                           # confidence shown

    form = {f"plog:{k}": str(v)
            for k, v in CN_PLOG_PROPOSAL["columns"].items()}
    r2 = client.post(f"/remap/{token}/apply", data=form)
    assert r2.status_code == 200
    assert "Parse preview" in r2.text
    assert "Headers remapped" in r2.text           # audit trail on preview
    assert "cn_tracker.xlsx" in r2.text or "WAVE #1" in r2.text


def test_run_flow_required_field_missing_bounces_back(client):
    r = _upload_run(client, build_cn_plog_bytes())
    token = r.headers["location"].rsplit("/", 1)[1]
    form = {f"plog:{k}": str(v)
            for k, v in CN_PLOG_PROPOSAL["columns"].items() if k != "postlink"}
    r2 = client.post(f"/remap/{token}/apply", data=form)
    assert r2.status_code == 422
    assert "POST LINK" in r2.text                  # names the missing field
    assert token in remap_service.PENDING_MAPS         # still pending, fixable


def test_run_flow_reject_discards(client):
    r = _upload_run(client, build_cn_plog_bytes())
    token = r.headers["location"].rsplit("/", 1)[1]
    r2 = client.post(f"/remap/{token}/reject", follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == "/"
    assert token not in remap_service.PENDING_MAPS


def test_approved_mapping_is_cached_and_auto_applied(client):
    r = _upload_run(client, build_cn_plog_bytes())
    token = r.headers["location"].rsplit("/", 1)[1]
    form = {f"plog:{k}": str(v)
            for k, v in CN_PLOG_PROPOSAL["columns"].items()}
    client.post(f"/remap/{token}/apply", data=form)

    # same format again — no audit page, straight to preview, visibly noted
    r2 = _upload_run(client, build_cn_plog_bytes())
    assert r2.status_code == 200
    assert "Parse preview" in r2.text
    assert "Applied automatically" in r2.text
    assert "open-mode" in r2.text                  # who approved it


def test_no_api_key_keeps_plain_error(client, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    r = _upload_run(client, build_cn_plog_bytes())
    assert r.status_code == 422
    assert "PLOG parse failed" in r.text


# ----------------------------------------------------------- efficiency flow

CN_EFF_PROPOSAL = {
    "sheet": "达人列表", "header_row": 2,
    "columns": {"no": 1, "campaign": 3, "type": 4, "level": 5, "name": 6,
                "fanbase(k)": 7, "postdate": 8, "postlink": 10,
                "impression": 11, "like": 12, "collection": 13, "comment": 14,
                "ttlengagement": 15, "price": 16},
    "confidence": {}, "warnings": [],
}


def test_efficiency_flow_audit_then_approve_stays_in_memory(client, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(
        schema_map, "_call_llm",
        lambda system, user: json.dumps(CN_EFF_PROPOSAL, ensure_ascii=False))
    mime = ("application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet")
    r = client.post("/efficiency",
                    files={"report": ("cn_wave.xlsx", build_cn_plog_bytes(),
                                      mime)},
                    data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/remap/")
    token = r.headers["location"].rsplit("/", 1)[1]
    assert client.get(f"/remap/{token}").status_code == 200

    form = {f"eff:{k}": str(v) for k, v in CN_EFF_PROPOSAL["columns"].items()}
    r2 = client.post(f"/remap/{token}/apply", data=form,
                     follow_redirects=True)
    assert r2.status_code == 200
    assert "Headers remapped" in r2.text           # note on the report page
    assert "Download .pptx" in r2.text
    # privacy: nothing about this workbook ever lands on disk
    uploads = config.UPLOAD_DIR
    assert not uploads.exists() or not any(uploads.rglob("*cn_wave*"))


def test_expired_mapping_token_404s(client):
    r = _upload_run(client, build_cn_plog_bytes())
    token = r.headers["location"].rsplit("/", 1)[1]
    remap_service.PENDING_MAPS[token]["created"] -= remap_service.PENDING_MAPS.ttl_seconds + 1
    assert client.get(f"/remap/{token}").status_code == 404
    assert client.post(f"/remap/{token}/apply", data={}).status_code == 404
