from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.token_store import TokenStore, TokenStoreFull


def test_claim_has_exactly_one_concurrent_winner():
    store = TokenStore(ttl_seconds=60, max_entries=10)
    token = store.put({"value": 1})
    barrier = Barrier(8)

    def claim_at_once() -> str:
        barrier.wait()
        status, _entry = store.claim(token)
        return status

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _i: claim_at_once(), range(8)))

    assert statuses.count("claimed") == 1
    assert statuses.count("busy") == 7
    assert store.pop(token) is not None
    assert store.pop(token) is None
    assert store.claim(token) == ("missing", None)


def test_release_restores_an_unexpired_claim(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.core.token_store.time.time", lambda: now[0])
    store = TokenStore(ttl_seconds=10, max_entries=2)
    token = store.put({"value": 1})

    assert store.claim(token)[0] == "claimed"
    assert store.claim(token) == ("busy", None)
    assert store.release(token) is True
    assert store.claim(token)[0] == "claimed"

    now[0] += 11
    assert store.release(token) is False
    assert store.get(token) is None


def test_claimed_entry_is_not_evicted_by_size_cap():
    store = TokenStore(ttl_seconds=60, max_entries=1)
    claimed = store.put({"value": "in progress"})
    assert store.claim(claimed)[0] == "claimed"

    with pytest.raises(TokenStoreFull, match="busy"):
        store.put({"value": "waiting"})
    assert store.get(claimed) is not None
    assert len(store) == 1

    store.pop(claimed)
    replacement = store.put({"value": "new"})
    assert store.get(replacement) is not None


def test_claimed_entries_cannot_bypass_hard_cap():
    store = TokenStore(ttl_seconds=60, max_entries=2)
    for value in (1, 2):
        token = store.put({"value": value})
        assert store.claim(token)[0] == "claimed"
    for _ in range(5):
        with pytest.raises(TokenStoreFull):
            store.put({"value": "overflow"})
    assert len(store) == 2


def test_expiry_eviction_and_clear_discard_resources(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.core.token_store.time.time", lambda: now[0])
    discarded = []
    store = TokenStore(ttl_seconds=10, max_entries=1,
                       on_discard=lambda entry: discarded.append(entry["id"]))

    expired = store.put({"id": "expired"})
    now[0] += 11
    assert store.get(expired) is None
    assert discarded == ["expired"]

    store.put({"id": "evicted"})
    store.put({"id": "cleared"})
    assert discarded == ["expired", "evicted"]
    store.clear()
    assert discarded == ["expired", "evicted", "cleared"]


def test_active_expiry_reaps_without_a_get(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.core.token_store.time.time", lambda: now[0])
    discarded = []
    store = TokenStore(ttl_seconds=10, max_entries=2,
                       on_discard=lambda entry: discarded.append(entry["id"]))
    store.put({"id": "stale"})
    now[0] += 11
    assert store.discard_expired() == 1
    assert discarded == ["stale"] and len(store) == 0
