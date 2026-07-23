"""Account system: setup bootstrap, login, team management, session security."""
from __future__ import annotations


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


def test_session_cookie_is_secure_in_deployment(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    r = _setup_admin(client)
    assert "Secure" in r.headers["set-cookie"]


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


def test_password_change_revokes_existing_session(client):
    _setup_admin(client)
    stolen = client.cookies.get("dmr_session")
    assert stolen

    client.post("/team/password", data={"username": "boss",
                                        "password": "newpassword9"})
    client.cookies.clear()
    client.cookies.set("dmr_session", stolen)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


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


def test_missing_password_fails_closed(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ACCESS", False)
    r = client.get("/")
    assert r.status_code == 503


def test_runtime_requires_explicit_open_mode(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ACCESS", False)
    with pytest.raises(RuntimeError, match="APP_PASSWORD is required"):
        config.validate_runtime()
    monkeypatch.setattr(config, "ALLOW_OPEN_ACCESS", True)
    config.validate_runtime()


# ------------------------------------------------- signing-secret derivation

def test_secret_not_derived_from_setup_code(client, monkeypatch):
    """Regression: with APP_SECRET unset, a cookie signed with the old
    'dmr-' + APP_PASSWORD derivation must NOT validate — anyone holding the
    shared setup code could forge any user's session."""
    import hashlib
    import hmac as hmac_mod
    import time as time_mod
    from app import config
    from app.auth import service

    _setup_admin(client)
    monkeypatch.setattr(config, "APP_SECRET", "")
    service._secret_cache.clear()
    client.cookies.clear()
    payload = f"boss|{int(time_mod.time()) + 3600}"
    forged_sig = hmac_mod.new(("dmr-" + config.APP_PASSWORD).encode(),
                              payload.encode(), hashlib.sha256).hexdigest()
    client.cookies.set("dmr_session", f"{payload}|{forged_sig}")
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_generated_secret_is_persisted_and_survives_restart(client, monkeypatch):
    """With APP_SECRET unset a random secret is generated, written under
    DATA_DIR, and re-read after a 'restart' (cache cleared) so existing
    sessions keep validating."""
    from app import config
    from app.auth import service

    monkeypatch.setattr(config, "APP_SECRET", "")
    service._secret_cache.clear()
    _setup_admin(client)
    token = service.make_session("boss")
    secret_file = config.DATA_DIR / "session_secret"
    assert secret_file.exists() and len(secret_file.read_text().strip()) >= 64
    service._secret_cache.clear()          # simulate process restart
    assert service.read_session(token) == "boss"
    # a genuine login round-trip works end to end
    client.cookies.clear()
    r = client.post("/login", data={"username": "boss",
                                    "password": "password123"})
    assert r.status_code == 303
    assert client.get("/").status_code == 200


# ------------------------------------------------------------- throttling

@pytest.fixture(autouse=True)
def _fresh_throttle():
    from app.auth import throttle
    throttle.reset()
    yield
    throttle.reset()


def test_login_throttles_after_repeated_failures(client):
    _setup_admin(client)
    client.get("/logout")
    for _ in range(5):
        r = client.post("/login", data={"username": "boss",
                                        "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/login", data={"username": "boss",
                                    "password": "wrong-password"})
    assert r.status_code == 429
    assert "wait" in r.text and "seconds" in r.text
    # even the CORRECT password is refused while blocked — guesses stay cheap
    r = client.post("/login", data={"username": "boss",
                                    "password": "password123"})
    assert r.status_code == 429


def test_login_success_resets_the_failure_count(client):
    _setup_admin(client)
    client.get("/logout")
    for _ in range(4):
        client.post("/login", data={"username": "boss", "password": "nope!"})
    r = client.post("/login", data={"username": "boss",
                                    "password": "password123"})
    assert r.status_code == 303                      # signed in
    client.get("/logout")
    for _ in range(4):                               # counter started over
        r = client.post("/login", data={"username": "boss", "password": "no"})
        assert r.status_code == 401


def test_setup_code_guessing_throttled(client):
    for _ in range(5):
        r = client.post("/setup", data={"code": "guess", "username": "x",
                                        "password": "password123"})
        assert r.status_code == 401
    r = client.post("/setup", data={"code": "guess", "username": "x",
                                    "password": "password123"})
    assert r.status_code == 429
    # …and the block applies even when the code is suddenly right
    r = client.post("/setup", data={"code": "setup-code-9", "username": "x",
                                    "password": "password123"})
    assert r.status_code == 429


def test_throttle_window_expires(client, monkeypatch):
    from app.auth import throttle
    _setup_admin(client)
    client.get("/logout")
    for _ in range(5):
        client.post("/login", data={"username": "boss", "password": "bad"})
    assert client.post("/login", data={
        "username": "boss", "password": "password123"}).status_code == 429
    real_time = throttle.time.time
    monkeypatch.setattr(throttle.time, "time",
                        lambda: real_time() + throttle.WINDOW_SECONDS + 1)
    assert client.post("/login", data={
        "username": "boss", "password": "password123"}).status_code == 303


def test_login_reserve_blocks_concurrent_guess_burst(client):
    """The reserve() gate must count in-flight guesses so a burst can't exceed
    the per-username cap while the awaited PBKDF2 is in flight."""
    from app.auth import throttle
    throttle.reset()
    _setup_admin(client)
    client.get("/logout")
    # simulate N coroutines that have all passed retry_after but not yet
    # registered — reserve() must let at most LIMITS['user'] through
    admitted = sum(1 for _ in range(20)
                   if throttle.reserve([("user", "boss"), ("ip", "1.2.3.4")]) == 0)
    assert admitted == throttle.LIMITS["user"]
    throttle.reset()


def test_login_success_releases_ip_reservation():
    """A correct login clears the user bucket and releases (does not keep) its
    own IP reservation, so the success is not counted as an IP failure."""
    from app.auth import throttle
    throttle.reset()
    # one reservation in each bucket (as a login would take)
    assert throttle.reserve([("user", "boss"), ("ip", "9.9.9.9")]) == 0
    throttle.clear("user", "boss")       # success: wipe user failures
    throttle.release("ip", "9.9.9.9")    # success: drop the ip reservation
    # ip bucket is now empty again — a shared IP isn't penalized for a success
    assert throttle.reserve([("ip", "9.9.9.9")]) == 0
    throttle.reset()
