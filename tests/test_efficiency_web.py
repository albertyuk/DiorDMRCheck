"""Web layer for the KOL efficiency report: upload → report view → download.

Verifies the privacy contract too: no upload lands on disk or in the DB, and
expired tokens 404.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_mod
from tests.fixtures import build_eff_bytes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")   # open mode — no login
    main_mod._EFF_REPORTS.clear()
    with TestClient(main_mod.app) as c:
        yield c
    main_mod._EFF_REPORTS.clear()


def _upload(client, **form):
    return client.post(
        "/efficiency",
        files={"report": ("wave1.xlsx", build_eff_bytes(),
                          "application/vnd.openxmlformats-officedocument"
                          ".spreadsheetml.sheet")},
        data=form, follow_redirects=True)


def test_full_flow(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    r = _upload(client)
    assert r.status_code == 200
    body = r.text
    assert "Download .pptx" in body
    assert "MID PAID" in body and "V10" in body            # findings shown WITH output
    assert "CPM WINNER" in body
    # nothing was written to disk — client campaign data stays in memory
    assert not (tmp_path / "uploads").exists()

    token = next(iter(main_mod._EFF_REPORTS))
    d = client.get(f"/efficiency/{token}/deck.pptx")
    assert d.status_code == 200
    assert d.content[:2] == b"PK"
    assert "wave1_efficiency.pptx" in d.headers["content-disposition"]


def test_expired_token_404(client):
    _upload(client)
    token = next(iter(main_mod._EFF_REPORTS))
    main_mod._EFF_REPORTS[token]["created"] -= main_mod._EFF_TTL_SECONDS + 1
    assert client.get(f"/efficiency/{token}").status_code == 404
    assert client.get(f"/efficiency/{token}/deck.pptx").status_code == 404


def test_blocked_report_has_no_deck(client):
    # the fixture has a V2 row; block policy turns it into an ERROR… but the
    # form doesn't expose the policy, so exercise the store contract directly
    from app.effreport import ReportConfig, analyze
    import io
    a = analyze(io.BytesIO(build_eff_bytes()),
                ReportConfig(missing_row_policy="block"))
    token = main_mod._eff_store({"analysis": a, "pptx": None, "filename": "x"})
    page = client.get(f"/efficiency/{token}")
    assert page.status_code == 200
    assert "Deck not generated" in page.text
    assert client.get(f"/efficiency/{token}/deck.pptx").status_code == 404


def test_bad_file_is_422_not_500(client):
    r = client.post("/efficiency",
                    files={"report": ("junk.xlsx", b"not a zip", "application/zip")},
                    data={})
    assert r.status_code == 422


def test_invalid_config_values_fall_back_to_defaults(client):
    r = _upload(client, basis="nonsense", tier_mode="nonsense", language="xx")
    assert r.status_code == 200
    assert "Basis: pooled" in r.text


def test_demo_image_shipped_and_shown_per_language(client):
    """The feature-demo slide images are synthetic-data renders committed to
    /static — both language variants must exist and be wired to the UI lang."""
    from pathlib import Path
    static = Path(main_mod.__file__).parent / "static"
    for lang in ("en", "zh"):
        assert (static / f"eff_demo_{lang}.jpg").stat().st_size > 10_000
    assert "/static/eff_demo_en.jpg" in client.get("/").text
    assert "/static/eff_demo_en.jpg" in client.get("/efficiency").text
    client.cookies.set("dmr_lang", "zh")
    assert "/static/eff_demo_zh.jpg" in client.get("/").text
    assert "/static/eff_demo_zh.jpg" in client.get("/efficiency").text


def test_store_cap_evicts_oldest():
    main_mod._EFF_REPORTS.clear()
    tokens = [main_mod._eff_store({"analysis": {}, "pptx": None,
                                   "filename": str(i)})
              for i in range(main_mod._EFF_MAX + 3)]
    assert len(main_mod._EFF_REPORTS) <= main_mod._EFF_MAX
    assert tokens[0] not in main_mod._EFF_REPORTS      # oldest evicted
    assert tokens[-1] in main_mod._EFF_REPORTS
    main_mod._EFF_REPORTS.clear()
