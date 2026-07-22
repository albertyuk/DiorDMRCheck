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


def _run_id(response):
    match = re.search(r"/runs/([0-9a-f]+)/start", response.text)
    assert match, "preview page should offer the start action"
    return match.group(1)


def _upload_with_perimeter(client, tmp_path, *, filename="perim.xlsx", data=None):
    plog = tmp_path / "p.xlsx"
    dmr = tmp_path / "d.xlsx"
    fixtures.build_plog(str(plog))
    fixtures.build_dmr(str(dmr))
    perim = data if data is not None else build_perimeter_bytes()
    return client.post("/upload", files={
        "plog": ("p.xlsx", plog.read_bytes(), MIME),
        "dmr": ("d.xlsx", dmr.read_bytes(), MIME),
        "perimeter": (filename, perim, MIME),
    }), pm.file_hash(perim)


def _upload_without_perimeter(client, tmp_path):
    plog = tmp_path / "p.xlsx"
    dmr = tmp_path / "d.xlsx"
    fixtures.build_plog(str(plog))
    fixtures.build_dmr(str(dmr))
    return client.post("/upload", files={
        "plog": ("p.xlsx", plog.read_bytes(), MIME),
        "dmr": ("d.xlsx", dmr.read_bytes(), MIME),
    })


def test_perimeter_promotes_only_on_run_start(client, tmp_path, monkeypatch):
    r, perim_hash = _upload_with_perimeter(client, tmp_path)
    assert r.status_code == 200
    run_id = _run_id(r)

    # preview rendered, perimeter cached — but NOT current yet
    assert pm.current_meta() is None

    started = []
    monkeypatch.setattr(runs, "start_run", lambda rid: started.append(rid))
    r2 = client.post(f"/runs/{run_id}/start", data={}, follow_redirects=False)
    assert r2.status_code == 303 and started == [run_id]

    cur = pm.current_meta()
    assert cur and cur["hash"] == perim_hash      # promoted at confirmation

    from app.core import db
    run = db.run_get(run_id)
    assert run["perimeter_uploaded"] == 1
    assert run["perimeter_name"] == "perim.xlsx"


def test_inherited_perimeter_never_rolls_back_new_default(client, tmp_path,
                                                          monkeypatch):
    """A no-upload preview captures its run-local perimeter, but starting it
    later must not overwrite a newer explicit upload selected by another run."""
    old_data = build_perimeter_bytes(extraction="19/05/2026 10:30:00")
    old_meta, _ = pm.parse_and_cache(old_data, "old.xlsx")
    pm.promote_cached(old_meta["hash"], filename="old.xlsx")

    inherited_response = _upload_without_perimeter(client, tmp_path)
    assert inherited_response.status_code == 200
    inherited_id = _run_id(inherited_response)

    new_data = build_perimeter_bytes(extraction="20/05/2026 10:30:00")
    explicit_response, new_hash = _upload_with_perimeter(
        client, tmp_path, filename="new.xlsx", data=new_data)
    explicit_id = _run_id(explicit_response)

    started = []
    monkeypatch.setattr(runs, "start_run", lambda rid: started.append(rid))
    client.post(f"/runs/{explicit_id}/start", data={}, follow_redirects=False)
    assert pm.current_meta()["hash"] == new_hash

    client.post(f"/runs/{inherited_id}/start", data={}, follow_redirects=False)
    assert started == [explicit_id, inherited_id]
    assert pm.current_meta()["hash"] == new_hash

    from app.core import db
    inherited = db.run_get(inherited_id)
    assert inherited["perimeter_hash"] == old_meta["hash"]
    assert inherited["perimeter_uploaded"] == 0
    assert inherited["perimeter_name"] == "old.xlsx"


def test_cached_upload_keeps_run_filename_and_retry_does_not_promote(
        client, tmp_path, monkeypatch):
    """Content-addressed cache hits retain the new upload's display name, and
    retrying that run cannot overwrite a subsequently selected perimeter."""
    shared = build_perimeter_bytes(extraction="19/05/2026 10:30:00")
    first_response, shared_hash = _upload_with_perimeter(
        client, tmp_path, filename="first.xlsx", data=shared)
    second_response, _ = _upload_with_perimeter(
        client, tmp_path, filename="renamed.xlsx", data=shared)
    first_id, second_id = _run_id(first_response), _run_id(second_response)

    started = []
    monkeypatch.setattr(runs, "start_run", lambda rid: started.append(rid))
    client.post(f"/runs/{first_id}/start", data={}, follow_redirects=False)
    client.post(f"/runs/{second_id}/start", data={}, follow_redirects=False)
    assert pm.current_meta()["hash"] == shared_hash
    assert pm.current_meta()["filename"] == "renamed.xlsx"

    from app.core import db
    second = db.run_get(second_id)
    assert second["perimeter_name"] == "renamed.xlsx"
    cached = pm.load_cached(shared_hash, filename=second["perimeter_name"])
    assert cached and cached.filename == "renamed.xlsx"

    newer = build_perimeter_bytes(extraction="21/05/2026 10:30:00")
    newer_meta, _ = pm.parse_and_cache(newer, "newest.xlsx")
    pm.promote_cached(newer_meta["hash"], filename="newest.xlsx")
    db.run_update(second_id, status="error")

    client.post(f"/runs/{second_id}/start", data={}, follow_redirects=False)
    assert started == [first_id, second_id, second_id]
    assert pm.current_meta()["hash"] == newer_meta["hash"]
    assert pm.current_meta()["filename"] == "newest.xlsx"


def test_preview_offers_editable_export_window(client, tmp_path):
    import tempfile, os
    from tests import fixtures
    fd, pp = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
    fd, dp = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
    fixtures.build_plog(pp)
    fixtures.build_dmr(dp)
    mime = ("application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet")
    r = client.post("/upload", files={
        "plog": ("p.xlsx", open(pp, "rb").read(), mime),
        "dmr": ("d.xlsx", open(dp, "rb").read(), mime)})
    os.unlink(pp); os.unlink(dp)
    assert r.status_code == 200
    body = r.text
    # date inputs exist and are prefilled with the metadata-detected window
    assert 'name="window_from"' in body and 'name="window_to"' in body
    assert 'value="2026-01-01"' in body and 'value="2026-07-20"' in body
    assert "Clear either date to disable the window checks" in body
