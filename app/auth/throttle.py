"""Failure throttling for the credential endpoints (/login, /setup).

Sliding 5-minute window per username and per client IP, in process memory —
consistent with the app's documented single-process constraint. PBKDF2 at
200k iterations costs ~16 ms per guess, so an unthrottled attacker gets
~60 serial guesses/second/core; these limits cap that at a handful per
window while never locking out a legitimate user for long.

The IP key prefers Fly's edge-set ``Fly-Client-IP`` header (not spoofable
through the Fly proxy) and falls back to the socket peer for local runs.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

WINDOW_SECONDS = 5 * 60
LIMITS = {          # scope → max failures inside the window
    "user": 5,      # per username on /login
    "ip": 20,       # per client IP on /login (many usernames)
    "setup": 5,     # per client IP on /setup (setup-code guessing)
}
_MAX_KEYS = 10_000  # hard memory bound under distributed abuse

_LOCK = threading.Lock()
_failures: dict[tuple[str, str], list[float]] = {}


def client_ip(request) -> str:
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "")
            or "unknown")


def _prune(now: float) -> None:
    dead = [k for k, ts in _failures.items()
            if not ts or now - ts[-1] > WINDOW_SECONDS]
    for k in dead:
        del _failures[k]
    while len(_failures) > _MAX_KEYS:   # oldest-last-failure first
        oldest = min(_failures, key=lambda k: _failures[k][-1])
        del _failures[oldest]


def retry_after(scope: str, key: str) -> int:
    """Seconds until another attempt is allowed (0 = not blocked)."""
    now = time.time()
    with _LOCK:
        _prune(now)
        ts = [t for t in _failures.get((scope, key), [])
              if now - t <= WINDOW_SECONDS]
        if len(ts) < LIMITS[scope]:
            return 0
        return max(1, int(WINDOW_SECONDS - (now - ts[0])) + 1)


def register_failure(scope: str, key: str) -> None:
    now = time.time()
    with _LOCK:
        ts = _failures.setdefault((scope, key), [])
        ts.append(now)
        del ts[:-LIMITS[scope]]         # only the last `limit` matter
        _prune(now)


def clear(scope: str, key: str) -> None:
    with _LOCK:
        _failures.pop((scope, key), None)


def reset() -> None:
    """Test hook."""
    with _LOCK:
        _failures.clear()
