"""Resolve-phase speedups: the direct-path circuit breaker and the pooled
TikHub client. Network is never touched — direct/TikHub calls are stubbed."""
from __future__ import annotations

import pytest

import app.reconciler.links as links
from app.reconciler.links import DIRECT_BREAKER, TikHubError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "links.sqlite3")
    monkeypatch.setattr(config, "TIKHUB_API_KEY", "test-key")
    DIRECT_BREAKER.reset()
    yield
    DIRECT_BREAKER.reset()


def test_breaker_trips_after_consecutive_failures_and_reprobes():
    for _ in range(DIRECT_BREAKER.TRIP_AFTER):
        assert DIRECT_BREAKER.should_try()
        DIRECT_BREAKER.record(False)
    # tripped: skipped until the re-probe slot
    tries = [DIRECT_BREAKER.should_try()
             for _ in range(DIRECT_BREAKER.REPROBE_EVERY)]
    assert tries.count(True) == 1 and tries[-1] is True
    # a success re-arms the path fully
    DIRECT_BREAKER.record(True)
    assert DIRECT_BREAKER.should_try()


def test_resolve_skips_direct_after_breaker_trips(monkeypatch):
    direct_calls = []
    monkeypatch.setattr(links, "direct_resolve",
                        lambda url: direct_calls.append(url) or None)
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **kw: (_ for _ in ()).throw(TikHubError("stub: no network")))

    n = DIRECT_BREAKER.TRIP_AFTER + 5
    for i in range(n):
        res = links.resolve_link(f"http://xhslink.com/speed{i}",
                                 retry_failed=True)
        assert res.status == "failed"           # tikhub stub always fails
    # only the first TRIP_AFTER attempts paid the direct-path cost
    assert len(direct_calls) == DIRECT_BREAKER.TRIP_AFTER


def test_direct_success_keeps_path_armed(monkeypatch):
    monkeypatch.setattr(links, "direct_resolve",
                        lambda url: "a" * 24)
    for i in range(10):
        res = links.resolve_link(f"http://xhslink.com/ok{i}")
        assert res.status == "ok" and res.source == "direct"
    assert DIRECT_BREAKER.should_try()          # never tripped


def test_ensure_author_respects_breaker(monkeypatch):
    page_calls = []
    monkeypatch.setattr(links, "direct_fetch_note_detail",
                        lambda url, expected_note_id="": page_calls.append(url) or {})
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **kw: (_ for _ in ()).throw(TikHubError("stub")))
    for _ in range(DIRECT_BREAKER.TRIP_AFTER):
        DIRECT_BREAKER.record(False)            # network already proven blocked
    from app.core import db
    url = "http://xhslink.com/e1"
    db.cache_put(url, status="ok", note_id="b" * 24, source="direct")
    res = links.Resolution(status="ok", note_id="b" * 24, source="direct")
    links.ensure_author(url, res, retry_failed=True)
    assert page_calls == []                     # free page fetch skipped


def test_tikhub_client_is_shared():
    assert links._tikhub_http() is links._tikhub_http()
