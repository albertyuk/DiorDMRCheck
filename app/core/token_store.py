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
from typing import Iterator, Optional


class TokenStore:
    def __init__(self, ttl_seconds: float, max_entries: int):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def put(self, entry: dict) -> str:
        token = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._entries.items()
                      if now - v["created"] > self.ttl_seconds]:
                del self._entries[k]
            while len(self._entries) >= self.max_entries:
                oldest = min(self._entries,
                             key=lambda k: self._entries[k]["created"])
                del self._entries[oldest]
            self._entries[token] = {**entry, "created": now}
        return token

    def get(self, token: str) -> Optional[dict]:
        """The entry, or None when unknown — or expired (expiry deletes it)."""
        with self._lock:
            entry = self._entries.get(token)
            if entry and time.time() - entry["created"] > self.ttl_seconds:
                del self._entries[token]
                return None
            return entry

    def pop(self, token: str) -> Optional[dict]:
        with self._lock:
            return self._entries.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # Introspection (tests, diagnostics). __getitem__ is raw: no TTL check.
    def __contains__(self, token: str) -> bool:
        return token in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, token: str) -> dict:
        return self._entries[token]
