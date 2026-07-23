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


def test_ensure_author_respects_its_own_breaker(monkeypatch):
    """ensure_author uses PAGE_BREAKER, independent of the resolve breaker."""
    from app.reconciler.links import PAGE_BREAKER
    page_calls = []
    monkeypatch.setattr(links, "direct_fetch_note_detail",
                        lambda url, expected_note_id="": page_calls.append(url) or {})
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **kw: (_ for _ in ()).throw(TikHubError("stub")))
    PAGE_BREAKER.reset()
    for _ in range(PAGE_BREAKER.TRIP_AFTER):
        PAGE_BREAKER.record(False)              # page fetch proven blocked
    from app.core import db
    url = "http://xhslink.com/e1"
    db.cache_put(url, status="ok", note_id="b" * 24, source="direct")
    res = links.Resolution(status="ok", note_id="b" * 24, source="direct")
    links.ensure_author(url, res, retry_failed=True)
    assert page_calls == []                     # free page fetch skipped
    PAGE_BREAKER.reset()


def test_resolve_and_page_breakers_are_independent():
    """A run of page-fetch misses must not starve the redirect-resolve path."""
    from app.reconciler.links import DIRECT_BREAKER, PAGE_BREAKER
    DIRECT_BREAKER.reset(); PAGE_BREAKER.reset()
    for _ in range(PAGE_BREAKER.TRIP_AFTER):
        PAGE_BREAKER.record(False)
    assert not PAGE_BREAKER.should_try()        # page breaker tripped…
    assert DIRECT_BREAKER.should_try()          # …resolve breaker unaffected
    DIRECT_BREAKER.reset(); PAGE_BREAKER.reset()


def test_tikhub_client_is_shared():
    assert links._tikhub_http() is links._tikhub_http()


def test_canonical_url_resolves_free_even_when_breaker_open(monkeypatch):
    """A xiaohongshu.com/explore/<id> link needs zero network — it must
    resolve directly and NOT be shunted to paid TikHub when the breaker is
    open, nor count as a network probe."""
    from app.reconciler.links import DIRECT_BREAKER
    import app.reconciler.links as links
    DIRECT_BREAKER.reset()
    for _ in range(DIRECT_BREAKER.TRIP_AFTER):
        DIRECT_BREAKER.record(False)            # breaker open (datacenter)
    tikhub_calls = []
    monkeypatch.setattr(links, "tikhub_fetch_note",
                        lambda **kw: tikhub_calls.append(kw) or {})
    nid = "a1b2c3d4e5f6a1b2c3d4e5f6"
    res = links.resolve_link(f"https://www.xiaohongshu.com/explore/{nid}")
    assert res.status == "ok" and res.note_id == nid and res.source == "direct"
    assert tikhub_calls == []                    # never billed
    assert DIRECT_BREAKER.should_try() is False  # zero-network resolve didn't re-close it
    DIRECT_BREAKER.reset()
