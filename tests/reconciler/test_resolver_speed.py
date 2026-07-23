"""Resolve-phase speedups: the direct-path circuit breaker and the pooled
TikHub client. Network is never touched — direct/TikHub calls are stubbed."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import app.reconciler.links as links
from app.reconciler.links import (DETAIL_BREAKER, RESOLVE_BREAKER,
                                  TikHubError)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "links.sqlite3")
    monkeypatch.setattr(config, "TIKHUB_API_KEY", "test-key")
    links.close_tikhub_client()
    RESOLVE_BREAKER.reset()
    DETAIL_BREAKER.reset()
    yield
    links.close_tikhub_client()
    RESOLVE_BREAKER.reset()
    DETAIL_BREAKER.reset()


def test_breaker_trips_after_consecutive_failures_and_reprobes():
    for _ in range(RESOLVE_BREAKER.TRIP_AFTER):
        assert RESOLVE_BREAKER.should_try()
        RESOLVE_BREAKER.record_network_failure()
    # tripped: skipped until the re-probe slot
    tries = [RESOLVE_BREAKER.should_try()
             for _ in range(RESOLVE_BREAKER.REPROBE_EVERY)]
    assert tries.count(True) == 1 and tries[-1] is True
    # a success re-arms the path fully
    RESOLVE_BREAKER.record_network_success()
    assert RESOLVE_BREAKER.should_try()


def test_resolve_skips_direct_after_breaker_trips(monkeypatch):
    direct_calls = []
    monkeypatch.setattr(links, "direct_resolve",
                        lambda url: direct_calls.append(url) or None)
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **kw: (_ for _ in ()).throw(TikHubError("stub: no network")))

    for _ in range(RESOLVE_BREAKER.TRIP_AFTER):
        RESOLVE_BREAKER.record_network_failure()
    for i in range(5):
        res = links.resolve_link(f"http://xhslink.com/speed{i}",
                                 retry_failed=True)
        assert res.status == "failed"           # tikhub stub always fails
    assert direct_calls == []


def test_direct_success_keeps_path_armed(monkeypatch):
    monkeypatch.setattr(links, "direct_resolve",
                        lambda url: "a" * 24)
    for i in range(10):
        res = links.resolve_link(f"http://xhslink.com/ok{i}")
        assert res.status == "ok" and res.source == "direct"
    assert RESOLVE_BREAKER.should_try()          # never tripped


def test_ensure_author_respects_breaker(monkeypatch):
    page_calls = []
    monkeypatch.setattr(links, "direct_fetch_note_detail",
                        lambda url, expected_note_id="": page_calls.append(url) or {})
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **kw: (_ for _ in ()).throw(TikHubError("stub")))
    for _ in range(DETAIL_BREAKER.TRIP_AFTER):
        DETAIL_BREAKER.record_network_failure()
    from app.core import db
    url = "https://xhslink.com/e1"
    db.cache_put(url, status="ok", note_id="b" * 24, source="direct")
    res = links.Resolution(status="ok", note_id="b" * 24, source="direct")
    links.ensure_author(url, res, retry_failed=True)
    assert page_calls == []                     # free page fetch skipped


def test_only_transport_failures_trip_resolve_breaker(monkeypatch):
    real_client = httpx.Client

    def blocked(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("blocked", request=request)

    def patched_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(blocked)
        return real_client(**kwargs)

    monkeypatch.setattr(links.httpx, "Client", patched_client)
    for i in range(RESOLVE_BREAKER.TRIP_AFTER):
        assert links.direct_resolve(f"https://xhslink.com/blocked{i}") is None
    assert not RESOLVE_BREAKER.should_try()


def test_completed_content_misses_do_not_trip_resolve_breaker(monkeypatch):
    real_client = httpx.Client

    def no_note(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="no note here", request=request)

    def patched_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(no_note)
        return real_client(**kwargs)

    monkeypatch.setattr(links.httpx, "Client", patched_client)
    for i in range(RESOLVE_BREAKER.TRIP_AFTER + 2):
        assert links.direct_resolve(f"https://xhslink.com/missing{i}") is None
    assert RESOLVE_BREAKER.should_try()


def test_direct_note_url_bypasses_open_network_breaker(monkeypatch):
    note_id = "6a0000000000000000000001"
    for _ in range(RESOLVE_BREAKER.TRIP_AFTER):
        RESOLVE_BREAKER.record_network_failure()
    monkeypatch.setattr(
        links, "direct_resolve",
        lambda _url: pytest.fail("zero-I/O note extraction was skipped"),
    )
    monkeypatch.setattr(
        links, "tikhub_fetch_note",
        lambda **_kwargs: pytest.fail("TikHub should not be needed"),
    )
    result = links.resolve_link(
        f"https://www.xiaohongshu.com/explore/{note_id}"
    )
    assert result.status == "ok"
    assert result.note_id == note_id
    assert result.source == "direct"


def test_url_canonicalization_coalesces_equivalent_concurrent_links(
        monkeypatch):
    note_id = "6a0000000000000000000001"
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_direct(_url):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return note_id

    monkeypatch.setattr(links, "direct_resolve", fake_direct)
    variants = (
        "https://XHSLINK.COM.:443/o/shared#first",
        "https://xhslink.com/o/shared#second",
    )
    assert {links.normalize_url(url) for url in variants} == {
        "https://xhslink.com/o/shared"
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(links.resolve_link, variants[0])
        assert entered.wait(1)
        second = executor.submit(links.resolve_link, variants[1])
        release.set()
        results = [first.result(2), second.result(2)]
    assert calls == 1
    assert all(result.note_id == note_id for result in results)
    assert not links._url_flights


def test_concurrent_forced_retries_share_new_failure(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    paid_calls = 0

    monkeypatch.setattr(links, "direct_resolve", lambda _url: None)

    def failed_tikhub(**_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        entered.set()
        assert release.wait(2)
        raise TikHubError("upstream failure detail")

    monkeypatch.setattr(links, "tikhub_fetch_note", failed_tikhub)
    url = "https://xhslink.com/o/retry"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            links.resolve_link, url, retry_failed=True
        )
        assert entered.wait(1)
        second = executor.submit(
            links.resolve_link, url, retry_failed=True
        )
        release.set()
        results = [first.result(2), second.result(2)]
    assert paid_calls == 1
    assert all(result.status == "failed" for result in results)
    assert all(result.error == "TikHub note lookup failed"
               for result in results)


def test_tikhub_client_is_shared():
    assert links._tikhub_http() is links._tikhub_http()


def test_tikhub_client_can_be_closed_and_recreated():
    first = links._tikhub_http()

    links.close_tikhub_client()

    assert first.is_closed
    second = links._tikhub_http()
    assert second is not first
    assert not second.is_closed
