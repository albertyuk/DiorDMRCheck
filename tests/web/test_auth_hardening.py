"""Regression tests for browser-origin and credential-oracle defenses."""
from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "auth-hardening.sqlite3")
    monkeypatch.setattr(config, "APP_PASSWORD", "setup-code-9")
    monkeypatch.setattr(config, "APP_SECRET", "test-session-secret")
    from app.auth import throttle
    from app.main import app
    from fastapi.testclient import TestClient

    throttle.reset()
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    throttle.reset()


def _setup(client):
    return client.post("/setup", data={
        "code": "setup-code-9", "username": "boss",
        "password": "password123",
    })


def test_unknown_user_takes_the_password_hash_path(client, monkeypatch):
    from app.auth import service

    _setup(client)
    client.cookies.clear()
    seen = []

    def verify(_password, stored):
        seen.append(stored)
        return False

    monkeypatch.setattr(service, "verify_password", verify)
    response = client.post("/login", data={
        "username": "not-a-user", "password": "guess",
    })
    assert response.status_code == 401
    assert seen == [service.DUMMY_PASSWORD_HASH]


def test_privileged_post_rejects_sibling_origin(client):
    from app.core import db

    _setup(client)
    response = client.post(
        "/team/add",
        data={"username": "victim", "password": "password123"},
        headers={
            "origin": "https://evil.example.com",
            "sec-fetch-site": "same-site",
        },
    )
    assert response.status_code == 403
    assert db.user_get("victim") is None


def test_same_origin_post_remains_allowed(client):
    from app.core import db

    _setup(client)
    response = client.post(
        "/team/add",
        data={"username": "mei", "password": "password123"},
        headers={
            "origin": "http://testserver",
            "sec-fetch-site": "same-origin",
        },
    )
    assert response.status_code == 303
    assert db.user_get("mei") is not None


def test_responses_disallow_cross_origin_framing(client):
    response = client.get("/login")

    assert response.headers["content-security-policy"] == (
        "frame-ancestors 'self'"
    )
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_logout_is_post_only_and_post_clears_session(client):
    _setup(client)
    session = client.cookies.get("dmr_session")
    assert session
    body = client.get("/").text
    assert '<form method="post" action="/logout">' in body
    assert 'href="/logout"' not in body

    get_response = client.get("/logout")

    assert get_response.status_code == 405
    assert client.cookies.get("dmr_session") == session
    assert client.get("/").status_code == 200

    post_response = client.post(
        "/logout",
        headers={
            "origin": "http://testserver",
            "sec-fetch-site": "same-origin",
        },
    )

    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/login"
    assert client.cookies.get("dmr_session") is None
    assert client.get("/").headers["location"] == "/login"


def test_logout_rejects_sibling_origin_without_clearing_session(client):
    _setup(client)
    session = client.cookies.get("dmr_session")

    response = client.post(
        "/logout",
        headers={
            "origin": "https://evil.example.com",
            "sec-fetch-site": "same-site",
        },
    )

    assert response.status_code == 403
    assert client.cookies.get("dmr_session") == session
    assert client.get("/").status_code == 200


def test_atomic_throttle_counts_in_flight_attempts():
    from app.auth import throttle

    throttle.reset()
    reservations = []
    blocked = []
    for _ in range(10):
        reservation, wait = throttle.reserve(
            ("user", "same-user"), ("ip", "192.0.2.1")
        )
        if reservation is None:
            blocked.append(wait)
        else:
            reservations.append(reservation)
    try:
        assert len(reservations) == throttle.LIMITS["user"]
        assert len(blocked) == 5
        assert all(wait > 0 for wait in blocked)
    finally:
        for reservation in reservations:
            throttle.complete(reservation, failed=True)
        throttle.reset()
