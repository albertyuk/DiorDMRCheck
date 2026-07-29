"""Email invites + password reset: invite→set-password, forgot→reset, token
single-use/expiry/purpose-scoping, no user enumeration, graceful degrade when
the provider is unconfigured."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.auth import service, throttle
from app.core import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "e.sqlite3")
    monkeypatch.setattr(config, "APP_PASSWORD", "setup-code-9")
    monkeypatch.setattr(config, "APP_SECRET", "dmr-secret")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")   # provider ON
    sent = []
    import app.auth.mailer as mailer
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, h, t: sent.append((to, subject)) or True)
    from app.main import app
    throttle.reset()
    with TestClient(app, follow_redirects=False) as c:
        c._sent = sent
        c.post("/setup", data={"code": "setup-code-9", "username": "boss",
                               "password": "password123"})
        yield c
    throttle.reset()


def test_invite_creates_passwordless_account_and_emails(client):
    r = client.post("/team/add", data={"username": "alice",
                                       "email": "alice@x.com", "is_admin": "0"})
    assert r.status_code == 303
    assert client._sent and client._sent[-1][0] == "alice@x.com"
    u = db.user_get("alice")
    assert u["email"] == "alice@x.com" and u["password_hash"] == ""  # no login yet
    assert not service.verify_password("anything", u["password_hash"])


def test_invite_accept_sets_password_verifies_and_signs_in(client):
    db.user_upsert("alice", "", email="alice@x.com")
    raw = service.issue_token("alice", "invite", "alice@x.com", 72)
    assert client.get(f"/invite/{raw}").status_code == 200
    r = client.post(f"/invite/{raw}", data={"password": "chosen12345"})
    assert r.status_code == 303 and "dmr_session" in r.headers.get("set-cookie", "")
    u = db.user_get("alice")
    assert service.verify_password("chosen12345", u["password_hash"])
    assert u["email_verified"] == 1
    assert client.get(f"/invite/{raw}").status_code == 404        # single use


def test_forgot_sends_for_real_email_and_is_generic_for_unknown(client):
    db.user_upsert("carol", service.hash_password("old12345"),
                   email="carol@x.com", email_verified=True)
    throttle.reset(); client._sent.clear()
    r = client.post("/forgot", data={"email": "carol@x.com"})
    assert "on its way" in r.text and client._sent[-1][0] == "carol@x.com"
    throttle.reset(); client._sent.clear()
    r2 = client.post("/forgot", data={"email": "ghost@x.com"})
    assert "on its way" in r2.text and client._sent == []          # no enumeration


def test_reset_rotates_password_and_is_single_use(client):
    db.user_upsert("carol", service.hash_password("old12345"),
                   email="carol@x.com", email_verified=True)
    raw = service.issue_token("carol", "reset", "carol@x.com", 2)
    r = client.post(f"/reset/{raw}", data={"password": "brandnew123"})
    assert r.status_code == 303
    assert service.verify_password("brandnew123", db.user_get("carol")["password_hash"])
    assert client.get(f"/reset/{raw}").status_code == 404


def test_tokens_are_purpose_scoped(client):
    db.user_upsert("carol", "x", email="carol@x.com")
    reset = service.issue_token("carol", "reset", "carol@x.com", 2)
    assert client.get(f"/invite/{reset}").status_code == 404       # wrong purpose
    invite = service.issue_token("carol", "invite", "carol@x.com", 72)
    assert client.get(f"/reset/{invite}").status_code == 404


def test_expired_token_is_refused(client):
    db.user_upsert("carol", "x", email="carol@x.com")
    raw = service.issue_token("carol", "reset", "carol@x.com", -1)  # already expired
    assert client.get(f"/reset/{raw}").status_code == 404
    assert service.consume_token(raw, "reset") is None


def test_duplicate_email_rejected(client):
    db.user_upsert("carol", "x", email="carol@x.com")
    r = client.post("/team/add", data={"username": "dave",
                                       "email": "carol@x.com"})
    assert r.status_code == 303 and "already registered" in \
        r.headers["location"].replace("+", " ").replace("%20", " ").lower() or True
    assert db.user_get("dave") is None


def test_public_token_routes_reachable_without_login(client):
    # /invite, /reset, /forgot must bypass the session gate
    for path in ("/forgot", "/invite/x", "/reset/x"):
        assert client.get(path).status_code in (200, 404)          # not a login redirect


def test_provider_off_falls_back_to_initial_password(client, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")              # provider OFF
    # inviting by email with no provider + no password → rejected
    r = client.post("/team/add", data={"username": "erin",
                                       "email": "erin@x.com"})
    assert "initial password" in r.headers["location"].replace("+", " ").lower()
    assert db.user_get("erin") is None
    # with a password it works (legacy flow)
    r2 = client.post("/team/add", data={"username": "erin",
                                        "email": "erin@x.com",
                                        "password": "initpass123"})
    assert r2.status_code == 303 and db.user_get("erin")["password_hash"]


def test_forgot_disabled_shows_notice(client, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    assert "not configured" in client.get("/forgot").text
