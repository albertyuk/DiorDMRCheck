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
import uuid
from dataclasses import dataclass

WINDOW_SECONDS = 5 * 60
LIMITS = {          # scope → max failures inside the window
    "user": 5,      # per username on /login
    "ip": 20,       # per client IP on /login (many usernames)
    "setup": 5,     # per client IP on /setup (setup-code guessing)
}
_MAX_KEYS = 10_000  # hard memory bound under distributed abuse

_LOCK = threading.Lock()
_failures: dict[tuple[str, str], list[float]] = {}
_pending: dict[tuple[str, str], dict[str, float]] = {}


@dataclass(frozen=True)
class Reservation:
    """One attempt provisionally charged against every applicable limit."""

    token: str
    keys: tuple[tuple[str, str], ...]


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
    for key, attempts in list(_pending.items()):
        for token, started in list(attempts.items()):
            if now - started > WINDOW_SECONDS:
                attempts.pop(token, None)
        if not attempts:
            _pending.pop(key, None)


def _wait_locked(scope: str, key: str, now: float) -> int:
    failures = [t for t in _failures.get((scope, key), [])
                if now - t <= WINDOW_SECONDS]
    pending = list(_pending.get((scope, key), {}).values())
    attempts = sorted([*failures, *pending])
    if len(attempts) < LIMITS[scope]:
        return 0
    return max(1, int(WINDOW_SECONDS - (now - attempts[0])) + 1)


def reserve(*keys: tuple[str, str]) -> tuple[Reservation | None, int]:
    """Atomically reserve capacity for one credential attempt.

    Counting in-flight attempts closes the check-then-hash race where a burst
    could all pass ``retry_after`` before any request registered its failure.
    The caller must always finish a successful reservation with ``complete``.
    """
    if not keys:
        raise ValueError("at least one throttle key is required")
    for scope, _key in keys:
        if scope not in LIMITS:
            raise ValueError(f"unknown throttle scope {scope!r}")
    now = time.time()
    with _LOCK:
        _prune(now)
        wait = max(_wait_locked(scope, key, now) for scope, key in keys)
        if wait:
            return None, wait
        reservation = Reservation(uuid.uuid4().hex, tuple(keys))
        for key in reservation.keys:
            _pending.setdefault(key, {})[reservation.token] = now
        return reservation, 0


def complete(reservation: Reservation, *, failed: bool,
             clear_scopes: tuple[str, ...] = ()) -> None:
    """Commit a reserved attempt as a failure, or release it on success."""
    now = time.time()
    with _LOCK:
        for key in reservation.keys:
            attempts = _pending.get(key)
            if attempts is not None:
                attempts.pop(reservation.token, None)
                if not attempts:
                    _pending.pop(key, None)
            if failed:
                ts = _failures.setdefault(key, [])
                ts.append(now)
                del ts[:-LIMITS[key[0]]]
        if not failed:
            for scope, key in reservation.keys:
                if scope in clear_scopes:
                    _failures.pop((scope, key), None)
        _prune(now)


def retry_after(scope: str, key: str) -> int:
    """Seconds until another attempt is allowed (0 = not blocked)."""
    now = time.time()
    with _LOCK:
        _prune(now)
        return _wait_locked(scope, key, now)


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
        _pending.clear()
