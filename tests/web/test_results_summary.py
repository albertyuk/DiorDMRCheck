"""Results-page summary: the Total posts stat and the DMR engagement
snapshot line (including runs stored before the dmr_* fields existed)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_mod
from app.core import db
from app.reconciler.parsers import parse_dmr, parse_plog
from app.reconciler.pipeline import run_pipeline, status_counts


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "web.sqlite3")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(main_mod.app) as c:
        yield c


def _done_run(client, plog_path, dmr_path, verdict_docs):
    run_id = "cafe00000001"
    db.run_create(run_id, plog_path=plog_path, dmr_path=dmr_path)
    db.run_update(run_id, status="done", result_json=json.dumps({
        "verdicts": verdict_docs,
        "counts": {"MATCH": 1},
        "plog_meta": {"header_row": 1, "sheet": "MASTER KOL LIST"},
        "dmr_meta": {},
    }, default=str))
    return run_id


def test_total_posts_and_dmr_snapshot(client, plog_path, dmr_path,
                                      fake_resolver):
    verdicts = run_pipeline(parse_plog(plog_path), parse_dmr(dmr_path))
    docs = [v.to_dict() for v in verdicts]
    run_id = "cafe00000002"
    db.run_create(run_id, plog_path=plog_path, dmr_path=dmr_path)
    db.run_update(run_id, status="done", result_json=json.dumps({
        "verdicts": docs, "counts": status_counts(verdicts),
        "plog_meta": {"header_row": 1, "sheet": "MASTER KOL LIST"},
        "dmr_meta": {},
    }, default=str))

    r = client.get(f"/runs/{run_id}/results")
    assert r.status_code == 200
    assert "Total posts" in r.text
    assert f"<b>{len(docs)}</b> Total posts" in r.text
    # matched rows expose the DMR engagement snapshot (weighted eng included)
    assert "DMR snapshot:" in r.text and "WEIGHTED ENG. 14.5" in r.text


def test_results_render_for_runs_stored_before_dmr_fields(client, plog_path,
                                                          dmr_path,
                                                          fake_resolver):
    """Historical result documents lack dmr_share_favorites &c. — the page
    must still render, showing ? placeholders in the snapshot line."""
    verdicts = run_pipeline(parse_plog(plog_path), parse_dmr(dmr_path))
    docs = [v.to_dict() for v in verdicts]
    legacy = [{k: val for k, val in d.items()
               if k not in ("dmr_share_favorites", "dmr_comments",
                            "dmr_engagement", "dmr_weighted_eng")}
              for d in docs]
    run_id = _done_run(client, plog_path, dmr_path, legacy)

    r = client.get(f"/runs/{run_id}/results")
    assert r.status_code == 200
    assert "Total posts" in r.text
    assert "DMR snapshot:" in r.text and "WEIGHTED ENG. ?" in r.text
