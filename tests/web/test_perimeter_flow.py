"""Web flow: an uploaded perimeter becomes current only when the run starts."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_mod
from app.reconciler import perimeter as pm, runs
from tests import fixtures
from tests.reconciler.test_perimeter import build_perimeter_bytes

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "web.sqlite3")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(main_mod.app) as c:
        yield c


def _upload_with_perimeter(client, tmp_path):
    plog = tmp_path / "p.xlsx"
    dmr = tmp_path / "d.xlsx"
    fixtures.build_plog(str(plog))
    fixtures.build_dmr(str(dmr))
    perim = build_perimeter_bytes()
    return client.post("/upload", files={
        "plog": ("p.xlsx", plog.read_bytes(), MIME),
        "dmr": ("d.xlsx", dmr.read_bytes(), MIME),
        "perimeter": ("perim.xlsx", perim, MIME),
    }), pm.file_hash(perim)


def test_perimeter_promotes_only_on_run_start(client, tmp_path, monkeypatch):
    r, perim_hash = _upload_with_perimeter(client, tmp_path)
    assert r.status_code == 200
    m = re.search(r"/runs/([0-9a-f]+)/start", r.text)
    assert m, "preview page should offer the start action"
    run_id = m.group(1)

    # preview rendered, perimeter cached — but NOT current yet
    assert pm.current_meta() is None

    started = []
    monkeypatch.setattr(runs, "start_run", lambda rid: started.append(rid))
    r2 = client.post(f"/runs/{run_id}/start", data={}, follow_redirects=False)
    assert r2.status_code == 303 and started == [run_id]

    cur = pm.current_meta()
    assert cur and cur["hash"] == perim_hash      # promoted at confirmation
