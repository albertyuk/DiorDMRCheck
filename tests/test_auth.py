"""Account system: setup bootstrap, login, team management, session security."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(config, "APP_PASSWORD", "setup-code-9")
    monkeypatch.setattr(config, "APP_SECRET", "dmr-setup-code-9")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _setup_admin(client, username="boss", password="password123"):
    return client.post("/setup", data={
        "code": "setup-code-9", "username": username, "password": password,
        "display": "The Boss"})


def test_no_users_redirects_to_setup(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_setup_rejects_wrong_code(client):
    r = client.post("/setup", data={"code": "nope", "username": "boss",
                                    "password": "password123"})
    assert r.status_code == 401
    r = client.get("/")
    assert r.headers["location"] == "/setup"  # still no users


def test_setup_creates_admin_and_signs_in(client):
    r = _setup_admin(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "dmr_session" in r.cookies
    r = client.get("/")
    assert r.status_code == 200  # session accepted


def test_login_flow(client):
    _setup_admin(client)
    client.cookies.clear()
    r = client.get("/")
    assert r.headers["location"] == "/login"  # users exist → login, not setup
    r = client.post("/login", data={"username": "BOSS",  # case-insensitive
                                    "password": "password123"})
    assert r.status_code == 303
    r = client.post("/login", data={"username": "boss", "password": "wrong"})
    assert r.status_code == 401


def test_forged_session_rejected(client):
    _setup_admin(client)
    client.cookies.clear()
    client.cookies.set("dmr_session", "boss|9999999999|deadbeef")
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_admin_adds_coworker_who_can_sign_in(client):
    _setup_admin(client)
    r = client.post("/team/add", data={"username": "mei", "password": "meipassword",
                                       "display": "Mei", "is_admin": "0"})
    assert r.status_code == 303
    client.cookies.clear()
    r = client.post("/login", data={"username": "mei", "password": "meipassword"})
    assert r.status_code == 303
    # non-admin cannot add accounts
    r = client.post("/team/add", data={"username": "x1", "password": "xpassword1"})
    assert "Only+admins" in r.headers["location"] or "Only%20admins" in r.headers["location"]
    from app.core import db
    assert db.user_get("x1") is None


def test_cannot_delete_last_admin_or_self(client):
    _setup_admin(client)
    r = client.post("/team/delete", data={"username": "boss"})
    assert "cannot" in r.headers["location"].lower() or "own" in r.headers["location"].lower()
    from app.core import db
    assert db.user_get("boss") is not None


def test_password_change_and_reset(client):
    _setup_admin(client)
    client.post("/team/add", data={"username": "mei", "password": "meipassword"})
    # admin resets mei's password
    r = client.post("/team/password", data={"username": "mei",
                                            "password": "newpassword9"})
    assert r.status_code == 303
    client.cookies.clear()
    assert client.post("/login", data={"username": "mei",
                                       "password": "newpassword9"}).status_code == 303
    # mei (non-admin) cannot reset boss's password
    r = client.post("/team/password", data={"username": "boss",
                                            "password": "hijacked999"})
    client.cookies.clear()
    assert client.post("/login", data={"username": "boss",
                                       "password": "password123"}).status_code == 303


def test_setup_recovers_admin_password(client):
    _setup_admin(client)
    client.cookies.clear()
    r = client.post("/setup", data={"code": "setup-code-9", "username": "boss",
                                    "password": "recovered999"})
    assert r.status_code == 303
    client.cookies.clear()
    assert client.post("/login", data={"username": "boss",
                                       "password": "recovered999"}).status_code == 303


def test_open_mode_without_app_password(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    r = client.get("/")
    assert r.status_code == 200
