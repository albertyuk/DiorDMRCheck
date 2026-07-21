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
                  "Efficiency report"):
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
    assert "知道了" in body
