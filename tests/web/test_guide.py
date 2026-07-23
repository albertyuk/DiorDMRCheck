"""First-visit guide modal: present on every page, auto-open flag off on the
auth pages, bilingual. (Whether the popup actually opens is client-side —
localStorage — so the server contract under test is the markup + flag.)"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    with TestClient(main_mod.app) as c:
        yield c


def test_guide_modal_and_button_on_index(client):
    body = client.get("/").text
    assert 'id="guide"' in body and "Quick guide" in body
    assert "openGuide()" in body and "closeGuide()" in body
    assert 'data-guide-auto="1"' in body
    # core content: workflow, verdict vocabulary, efficiency, engagement rule
    for probe in ("Running a reconciliation", "无帖子", "Check链接错误",
                  "Engagement numbers never decide a match",
                  "Efficiency report", "Unfamiliar sheet formats",
                  "Chinese market only"):
        assert probe in body, probe


def test_guide_present_but_not_auto_on_login(client):
    body = client.get("/login").text
    assert 'id="guide"' in body            # reachable via the button
    assert 'data-guide-auto="0"' in body   # but never auto-opens here


def test_guide_translates(client):
    client.cookies.set("dmr_lang", "zh")
    body = client.get("/").text
    assert "使用指南" in body and "如何发起核对" in body
    assert "判定结果怎么读" in body and "真正的 DMR 漏抓" in body
    assert "表格格式不认识怎么办" in body and "只评估中国市场" in body
    assert "知道了" in body


def test_file_requirements_blocks_on_upload_pages(client):
    body = client.get("/").text
    assert "What the files must contain" in body
    for probe in ("NAME", "POST LINK", "Blogger", "PostID", "List Micro",
                  "REDBOOK_ID", "24-character Xiaohongshu note id"):
        assert probe in body, probe
    eff = client.get("/efficiency").text
    assert "What the workbook must contain" in eff
    assert "TTL ENGAGEMENT" in eff and "PRICE" in eff
    client.cookies.set("dmr_lang", "zh")
    assert "文件需要满足什么要求" in client.get("/").text
    assert "工作簿需要满足什么要求" in client.get("/efficiency").text


# ------------------------------------------------------ streamlined-UI pass

def test_flow_stepper_on_upload_page(client):
    body = client.get("/").text
    assert 'id="flow-steps"' in body
    assert 'data-step="upload"' in body and "class=\"active" in body
    # the audit step is hidden unless it is the active one
    assert 'data-step="audit"' not in body


def test_upload_forms_have_dropzones_and_busy_states(client):
    body = client.get("/").text
    assert body.count('class="dropzone"') == 3          # plog, dmr, perimeter
    assert "data-busy-label" in body and "Uploading" in body
    eff = client.get("/efficiency").text
    assert 'class="dropzone"' in eff and "data-busy" in eff


def test_past_runs_show_badges_and_export_links(client):
    from app.core import db
    db.run_create("uitest0run01", plog_path="x", dmr_path="y",
                  plog_name="p.xlsx", dmr_name="d.xlsx", preview={},
                  perimeter_hash=None)
    db.run_update("uitest0run01", status="done", message="Run complete.")
    try:
        body = client.get("/").text
        assert "/runs/uitest0run01/export.xlsx" in body   # direct export
        assert 'class="badge match"' in body              # done badge
    finally:
        db.run_delete("uitest0run01")
