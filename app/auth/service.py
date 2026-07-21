"""Account authentication: PBKDF2 password hashing and HMAC-signed session
cookies. Stdlib only — no new dependencies.

Model: APP_PASSWORD is the *setup code*, not a login password. When no user
accounts exist yet, /setup (which requires the code) creates the first admin
account; the admin then adds coworkers on /team. /setup stays available as a
recovery path — anyone holding the setup code can create/reset an admin, so
the code should be treated as the root secret.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Optional

from .. import config

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 7 * 24 * 3600

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

_SECRET_LOCK = threading.Lock()
_secret_cache: dict[str, str] = {}  # keyed by file path (tests repoint DATA_DIR)


def signing_secret() -> str:
    """The session-cookie signing key.

    ``APP_SECRET`` when configured; otherwise a random secret generated once
    and persisted under ``DATA_DIR`` so sessions survive restarts. Never
    derived from ``APP_PASSWORD``: the setup code is shared with every
    teammate, and holding it must not allow forging session cookies.
    """
    if config.APP_SECRET:
        return config.APP_SECRET
    path = config.DATA_DIR / "session_secret"
    key = str(path)
    with _SECRET_LOCK:
        secret = _secret_cache.get(key)
        if secret:
            return secret
        config.ensure_dirs()
        secret = path.read_text().strip() if path.exists() else ""
        if not secret:
            secret = secrets.token_hex(32)
            path.write_text(secret)
            path.chmod(0o600)
        _secret_cache[key] = secret
        return secret


def normalize_username(username: str) -> str:
    return (username or "").strip().casefold()


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.fullmatch(username))


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                             PBKDF2_ITERATIONS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = (stored or "").split("$", 1)
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes,
                             PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), expected)


def _sign(payload: str) -> str:
    return hmac.new(signing_secret().encode(), payload.encode(),
                    hashlib.sha256).hexdigest()


def make_session(username: str) -> str:
    payload = f"{username}|{int(time.time()) + SESSION_TTL}"
    return f"{payload}|{_sign(payload)}"


def read_session(token: str) -> Optional[str]:
    """Return the username for a valid, unexpired session token, else None.
    Usernames cannot contain '|' (see USERNAME_RE), so rsplit is unambiguous."""
    if not token:
        return None
    try:
        username, exp, sig = token.rsplit("|", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(f"{username}|{exp}")):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return username
