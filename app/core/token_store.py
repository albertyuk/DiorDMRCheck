"""In-process token → entry store with TTL expiry and a size cap.

One parameterized implementation for every short-lived, session-like payload
the app hands the browser a token for (pending header-mapping audits,
finished efficiency reports). Entries are plain dicts; ``put`` stamps a
``created`` timestamp used for TTL expiry and oldest-first eviction.

CONSTRAINT: entries live in process memory — tokens do not survive a restart
and are invisible to other workers/machines. Deployments must run a single
process, or this class must grow a shared (e.g. SQLite) backing first.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Iterator, Literal, Optional


ClaimStatus = Literal["claimed", "busy", "missing"]


class TokenStoreFull(RuntimeError):
    """The bounded store has no unclaimed entry available for eviction."""


class TokenStore:
    def __init__(self, ttl_seconds: float, max_entries: int,
                 on_discard: Optional[Callable[[dict], None]] = None):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.on_discard = on_discard
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._claimed: set[str] = set()

    def _delete_locked(self, token: str) -> Optional[dict]:
        self._claimed.discard(token)
        return self._entries.pop(token, None)

    def _discard_locked(self, token: str) -> Optional[dict]:
        """Delete an abandoned entry and release any owned resources."""
        entry = self._delete_locked(token)
        if entry is not None and self.on_discard is not None:
            try:
                self.on_discard(entry)
            except Exception:
                # Cleanup must not break expiry or capacity enforcement.
                # Startup retention remains the final safety net.
                pass
        return entry

    def _live_entry_locked(self, token: str, now: float) -> Optional[dict]:
        entry = self._entries.get(token)
        # A request that claimed an entry before its deadline owns it until
        # it explicitly consumes or releases it. This keeps a long-running
        # continuation from expiring out from underneath itself.
        if (entry and token not in self._claimed
                and now - entry["created"] > self.ttl_seconds):
            self._discard_locked(token)
            return None
        return entry

    def put(self, entry: dict) -> str:
        token = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._entries.items()
                      if k not in self._claimed
                      and now - v["created"] > self.ttl_seconds]:
                self._discard_locked(k)
            while len(self._entries) >= self.max_entries:
                evictable = [k for k in self._entries
                             if k not in self._claimed]
                if not evictable:
                    raise TokenStoreFull(
                        f"token store is busy ({self.max_entries} active entries)"
                    )
                oldest = min(evictable,
                             key=lambda k: self._entries[k]["created"])
                self._discard_locked(oldest)
            self._entries[token] = {**entry, "created": now}
        return token

    def discard_expired(self) -> int:
        """Actively reap unclaimed TTL-expired entries."""
        now = time.time()
        with self._lock:
            expired = [token for token, entry in self._entries.items()
                       if token not in self._claimed
                       and now - entry["created"] > self.ttl_seconds]
            for token in expired:
                self._discard_locked(token)
            return len(expired)

    def get(self, token: str) -> Optional[dict]:
        """The entry, or None when unknown — or expired (expiry deletes it)."""
        with self._lock:
            return self._live_entry_locked(token, time.time())

    def claim(self, token: str) -> tuple[ClaimStatus, Optional[dict]]:
        """Atomically reserve a live entry for one consumer.

        A caller must eventually call :meth:`pop` after success or
        :meth:`release` after a retryable failure. Other consumers receive
        ``"busy"`` while the reservation is held and cannot run against the
        same payload concurrently.
        """
        with self._lock:
            entry = self._live_entry_locked(token, time.time())
            if entry is None:
                return "missing", None
            if token in self._claimed:
                return "busy", None
            self._claimed.add(token)
            return "claimed", entry

    def release(self, token: str) -> bool:
        """Release a claim after failure, preserving an unexpired entry."""
        with self._lock:
            if token not in self._claimed:
                return False
            self._claimed.remove(token)
            entry = self._entries.get(token)
            if (entry is not None
                    and time.time() - entry["created"] > self.ttl_seconds):
                self._discard_locked(token)
                return False
            return entry is not None

    def pop(self, token: str) -> Optional[dict]:
        with self._lock:
            return self._delete_locked(token)

    def clear(self) -> None:
        with self._lock:
            for token in list(self._entries):
                self._discard_locked(token)

    # Introspection (tests, diagnostics). __getitem__ is raw: no TTL check.
    def __contains__(self, token: str) -> bool:
        return token in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[dict]:
        """Stable shallow snapshots for lifecycle/diagnostic inspection."""
        with self._lock:
            return [dict(entry) for entry in self._entries.values()]

    def __getitem__(self, token: str) -> dict:
        return self._entries[token]
