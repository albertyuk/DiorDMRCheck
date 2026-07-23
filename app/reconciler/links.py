"""POST LINK → Xiaohongshu note resolution.

Two tiers, cached permanently on success:

1. Fast path — follow the xhslink.com redirect chain ourselves (redirects
   disabled, read Location headers) and regex the 24-hex note id out of the
   final xiaohongshu.com URL. Free, but likely blocked by XHS's WAF from
   datacenter IPs — failures degrade to the TikHub path.
2. Authoritative path — TikHub's XHS App-V2 note-detail endpoints, which
   accept the raw share URL directly via ``share_text`` (verified against
   api.tikhub.io/openapi.json at build time). Returns note_id, author id,
   nickname, engagement snapshot, title, publish time; the full payload is
   cached.

A resolved (note_id, author_id) never changes, so successful resolutions are
never re-fetched. Failures are cached too and only retried after a TTL or on
explicit request. Partial failure never raises out of resolve_link().
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .. import config
from ..core import db
from .domain import is_hex24

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# Global TikHub throttle: ≤ TIKHUB_CONCURRENCY requests in flight at once.
_tikhub_semaphore = threading.BoundedSemaphore(config.TIKHUB_CONCURRENCY)

# Process-wide, per-normalized-URL single-flight. The refcounted entries are
# removed when the last waiter leaves, so attacker-controlled unique URLs
# cannot grow a permanent lock registry.
_url_flights_guard = threading.Lock()
_url_flights: dict[str, tuple[threading.Lock, int]] = {}


@contextmanager
def _url_singleflight(url: str):
    with _url_flights_guard:
        lock, refs = _url_flights.get(url, (threading.Lock(), 0))
        _url_flights[url] = (lock, refs + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _url_flights_guard:
            current = _url_flights.get(url)
            if current and current[0] is lock:
                if current[1] <= 1:
                    _url_flights.pop(url, None)
                else:
                    _url_flights[url] = (lock, current[1] - 1)


@dataclass
class Resolution:
    status: str                      # ok | failed
    note_id: str = ""
    author_id: str = ""
    author_name: str = ""
    likes: Optional[int] = None
    collects: Optional[int] = None
    comments: Optional[int] = None
    title: str = ""
    publish_time: str = ""
    source: str = ""
    error: str = ""
    from_cache: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.note_id)


def _res_from_cache(row: dict) -> Resolution:
    raw = {}
    if row.get("raw_json"):
        try:
            raw = json.loads(row["raw_json"])
        except (ValueError, TypeError):
            raw = {}
    return Resolution(
        status=row["status"], note_id=row.get("note_id") or "",
        author_id=row.get("author_id") or "", author_name=row.get("author_name") or "",
        likes=row.get("likes"), collects=row.get("collects"),
        comments=row.get("comments"), title=row.get("title") or "",
        publish_time=row.get("publish_time") or "", source=row.get("source") or "",
        error=row.get("error") or "", from_cache=True, raw=raw,
    )


# ---------------------------------------------------------------- fast path

# Only these public Xiaohongshu-owned hosts may be fetched from workbook data.
# This is deliberately an allowlist rather than a private-IP denylist: the
# latter is vulnerable to redirects, unusual address spellings, and DNS
# rebinding.  Ports are restricted as well so a workbook cannot turn the
# reconciler into a proxy for unrelated services on an otherwise valid host.
_LINK_HOSTS = ("xhslink.com", "xiaohongshu.com")
_WEB_PORTS = {None, 80, 443}


def _allowed_link_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except (TypeError, httpx.InvalidURL):
        return False
    host = (parsed.host or "").rstrip(".").lower()
    return (
        parsed.scheme == "https"
        and parsed.port in _WEB_PORTS
        and any(host == root or host.endswith("." + root)
                for root in _LINK_HOSTS)
    )


# Only a note URL counts as resolved — profile pages etc. also carry 24-hex
# ids, and mistaking one for a note id would poison the permanent cache.
NOTE_PATH_RE = re.compile(
    r"^/(?:discovery/item|explore|items)/([0-9a-fA-F]{24})(?:/|$)"
)


def _note_id_from_url(url: str) -> Optional[str]:
    try:
        parsed = httpx.URL(url or "")
    except (TypeError, httpx.InvalidURL):
        return None
    host = (parsed.host or "").rstrip(".").lower()
    if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
        return None
    m = NOTE_PATH_RE.search(parsed.path)
    return m.group(1).lower() if m else None


def _note_object_from_state(state: Any) -> Optional[dict]:
    """Find the note object inside XHS's __INITIAL_STATE__. Known shapes:
    mobile/discovery SSR: state.noteData.data.noteData;
    desktop explore SSR:  state.note.noteDetailMap[<id>].note.
    Fallback: any dict carrying both a user.userId and interactInfo."""
    try:
        node = state["noteData"]["data"]["noteData"]
        if isinstance(node, dict) and node.get("user"):
            return node
    except (KeyError, TypeError):
        pass
    try:
        detail_map = state["note"]["noteDetailMap"]
        for entry in detail_map.values():
            node = entry.get("note")
            if isinstance(node, dict) and node.get("user"):
                return node
    except (KeyError, TypeError, AttributeError):
        pass
    stack = [state]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            user = node.get("user")
            if (isinstance(user, dict) and user.get("userId")
                    and ("interactInfo" in node or "time" in node)):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def direct_fetch_note_detail(url: str, expected_note_id: str = "") -> dict:
    """Free enrichment: fetch the note page itself and read the author (and
    engagement snapshot) out of the SSR __INITIAL_STATE__. Works only when
    XHS serves the full page to this IP — degrade to {} on any failure; the
    TikHub path remains authoritative."""
    try:
        current = normalize_url(url)
        with httpx.Client(follow_redirects=False,
                          timeout=config.DIRECT_HTTP_TIMEOUT * 2,
                          headers={"User-Agent": MOBILE_UA}) as client:
            for _hop in range(6):
                if not _allowed_link_url(current):
                    return {}
                resp = client.get(current)
                loc = resp.headers.get("location")
                if resp.status_code in (301, 302, 303, 307, 308) and loc:
                    current = str(httpx.URL(current).join(loc))
                    continue
                break
            else:
                return {}
        if resp.status_code != 200:
            return {}
        final_note = _note_id_from_url(str(resp.url))
        if expected_note_id and final_note and final_note != expected_note_id.lower():
            return {}  # redirected to a different note — don't trust the page
        m = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?)</script>",
                      resp.text, re.DOTALL)
        if not m:
            return {}
        blob = re.sub(r"\bundefined\b", "null", m.group(1).strip().rstrip(";"))
        note = _note_object_from_state(json.loads(blob))
        if not note:
            return {}
        embedded_note_id = str(note.get("noteId") or note.get("note_id") or "")
        if expected_note_id:
            # The final URL is insufficient proof: WAF/interstitial/home pages
            # can contain a different note object in __INITIAL_STATE__. Never
            # attach that object's author to the requested note.
            if (not is_hex24(embedded_note_id)
                    or embedded_note_id.lower() != expected_note_id.lower()):
                return {}
        user = note.get("user") or {}
        author_id = str(user.get("userId") or "")
        if not is_hex24(author_id):
            return {}
        interact = note.get("interactInfo") or {}
        ptime = note.get("time")
        if isinstance(ptime, (int, float)):
            ts = float(ptime) / (1000 if ptime > 1e12 else 1)
            try:
                ptime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except (OverflowError, OSError, ValueError):
                ptime = str(ptime)
        return {
            "note_id": embedded_note_id.lower(),
            "author_id": author_id,
            "author_name": str(user.get("nickname") or user.get("nickName") or ""),
            "likes": _to_int_or_none(interact.get("likedCount") or interact.get("liked_count")),
            "collects": _to_int_or_none(interact.get("collectedCount") or interact.get("collected_count")),
            "comments": _to_int_or_none(interact.get("commentCount") or interact.get("comment_count")),
            "title": str(note.get("title") or ""),
            "publish_time": str(ptime) if ptime else "",
        }
    except Exception:
        return {}


def direct_resolve(url: str) -> Optional[str]:
    """Walk the redirect chain manually; return the note id or None.

    Never raises — a malformed Location header, TLS error, or invalid URL is
    just a failed fast path (the TikHub path is authoritative anyway).
    """
    for attempt in range(config.DIRECT_HTTP_RETRIES + 1):
        try:
            current = normalize_url(url)
            with httpx.Client(
                follow_redirects=False,
                timeout=config.DIRECT_HTTP_TIMEOUT,
                headers={"User-Agent": MOBILE_UA},
            ) as client:
                for _hop in range(6):
                    if not _allowed_link_url(current):
                        return None
                    note = _note_id_from_url(current)
                    if note:
                        return note
                    resp = client.get(current)
                    loc = resp.headers.get("location")
                    if resp.status_code in (301, 302, 303, 307, 308) and loc:
                        current = str(httpx.URL(current).join(loc))
                        continue
                    # Landed. One last look at the final URL.
                    return _note_id_from_url(str(resp.url))
            return None
        except httpx.HTTPError:
            if attempt < config.DIRECT_HTTP_RETRIES:
                time.sleep(0.5 * (attempt + 1))
        except Exception:
            # e.g. httpx.InvalidURL from a garbage Location header — not
            # retryable, and must never escape into the run.
            return None
    return None


# -------------------------------------------------------------- TikHub path

def _walk(payload: Any, keys: tuple[str, ...]) -> Optional[Any]:
    """Depth-first search a nested payload for the first non-empty value under
    any of *keys*. TikHub's XHS payload shape varies by endpoint variant, so
    field extraction is deliberately tolerant."""
    stack = [payload]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for k in keys:
                if k in node and node[k] not in (None, "", [], {}):
                    return node[k]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _to_int_or_none(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        s = str(v).strip()
        if s.endswith(("万", "w", "W")):
            return int(float(s.rstrip("万wW")) * 10000)
        return int(float(s.replace(",", "")))
    except (ValueError, TypeError):
        return None


def _extract_note_fields(payload: dict) -> dict:
    note_id = _walk(payload, ("note_id", "noteId"))
    if not (isinstance(note_id, str) and is_hex24(note_id)):
        # Fall back to a bare "id" key only when it validates as a note id.
        # Deliberately NO regex-over-the-serialized-payload fallback: author
        # ids and trace ids are also 24-hex, and a wrong id would be cached
        # permanently as a success.
        note_id = _walk(payload, ("id",))
        if not (isinstance(note_id, str) and is_hex24(note_id)):
            note_id = None
    user = _walk(payload, ("user", "author", "user_info"))
    author_id = author_name = None
    if isinstance(user, dict):
        author_id = user.get("user_id") or user.get("userid") or user.get("id")
        author_name = user.get("nickname") or user.get("name") or user.get("nick_name")
    if not author_id:
        author_id = _walk(payload, ("user_id", "userId", "author_id"))
    if not author_name:
        author_name = _walk(payload, ("nickname", "nick_name", "author_name"))
    interact = _walk(payload, ("interact_info", "interactInfo"))
    likes = collects = comments = None
    if isinstance(interact, dict):
        likes = _to_int_or_none(interact.get("liked_count") or interact.get("likedCount"))
        collects = _to_int_or_none(interact.get("collected_count") or interact.get("collectedCount"))
        comments = _to_int_or_none(interact.get("comment_count") or interact.get("commentCount"))
    if likes is None:
        likes = _to_int_or_none(_walk(payload, ("liked_count", "likes", "like_count")))
    if collects is None:
        collects = _to_int_or_none(_walk(payload, ("collected_count", "collects", "collected")))
    if comments is None:
        comments = _to_int_or_none(_walk(payload, ("comment_count", "comments_count",)))
    title = _walk(payload, ("title", "display_title"))
    ptime = _walk(payload, ("time", "publish_time", "post_time", "create_time"))
    if isinstance(ptime, (int, float)):
        ts = float(ptime)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        try:
            ptime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except (OverflowError, OSError, ValueError):
            ptime = str(ptime)
    return {
        "note_id": (note_id or "").lower() if note_id else "",
        "author_id": str(author_id) if author_id else "",
        "author_name": str(author_name) if author_name else "",
        "likes": likes, "collects": collects, "comments": comments,
        "title": str(title) if title else "",
        "publish_time": str(ptime) if ptime else "",
    }


class TikHubError(Exception):
    """Terminal TikHub failure for one endpoint (4xx, bad payload, retries
    exhausted). The caller may still try the other note-detail endpoint."""


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Parse Retry-After (delta-seconds or HTTP-date), defaulting to backoff."""
    raw = resp.headers.get("retry-after", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
                return max(0.0, dt.timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    return float(2 ** (attempt + 1))


def _tikhub_get(path: str, params: dict, counter: Optional[Callable[[], None]]) -> dict:
    if not config.TIKHUB_API_KEY:
        raise TikHubError("TIKHUB_API_KEY not configured")
    url = config.TIKHUB_BASE_URL.rstrip("/") + path
    last_err = "no attempt made"
    for attempt in range(config.TIKHUB_MAX_RETRIES + 1):
        retryable = False
        try:
            with _tikhub_semaphore:
                if counter:
                    counter()
                with httpx.Client(timeout=config.TIKHUB_TIMEOUT) as client:
                    resp = client.get(
                        url, params=params,
                        headers={"Authorization": f"Bearer {config.TIKHUB_API_KEY}"},
                    )
            body = None
            if resp.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = resp.json()
                except ValueError:
                    body = None
            body_code = body.get("code") if isinstance(body, dict) else None

            more = attempt < config.TIKHUB_MAX_RETRIES
            if resp.status_code == 429 or body_code == 429:
                retryable = True
                last_err = "rate limited (429)"
                if more:
                    time.sleep(min(_retry_after_seconds(resp, attempt), 30))
            elif 400 <= resp.status_code < 500:
                raise TikHubError(f"TikHub {resp.status_code}: {resp.text[:300]}")
            elif resp.status_code >= 500:
                retryable = True
                last_err = f"TikHub {resp.status_code}"
                if more:
                    time.sleep(2 ** attempt)
            elif not isinstance(body, dict):
                raise TikHubError(f"TikHub returned a non-JSON-object body: {resp.text[:200]}")
            elif body_code is not None and body_code not in (200, 0):
                raise TikHubError(f"TikHub code {body_code}: {str(body)[:300]}")
            else:
                return body
        except (httpx.HTTPError, OSError) as e:
            retryable = True
            last_err = f"{type(e).__name__}: {e}"
            if attempt < config.TIKHUB_MAX_RETRIES:
                time.sleep(2 ** attempt)
        if not retryable:
            break
    raise TikHubError(f"TikHub request failed after retries: {last_err}")


def tikhub_fetch_note(share_url: str = "", note_id: str = "",
                      counter: Optional[Callable[[], None]] = None) -> dict:
    """Fetch note detail via TikHub. Notes can be image or video type and the
    App-V2 API splits them into two endpoints, so try image first then video.
    Returns the raw response body; raises TikHubError when both fail."""
    params: dict = {}
    if note_id:
        params["note_id"] = note_id
    elif share_url:
        params["share_text"] = share_url
    else:
        raise TikHubError("need share_url or note_id")
    errors = []
    for path in (config.TIKHUB_IMAGE_NOTE_PATH, config.TIKHUB_VIDEO_NOTE_PATH):
        try:
            return _tikhub_get(path, params, counter)
        except TikHubError as e:
            errors.append(f"{path.rsplit('/', 1)[-1]}: {e}")
    raise TikHubError(" ; ".join(errors))


# ----------------------------------------------------------- main entrypoint

def normalize_url(url: str) -> str:
    """Accept scheme-less cells like 'xhslink.com/o/abc' — resolvable in any
    browser and by TikHub's share_text. Always upgrade an initial XHS URL to
    HTTPS; redirect hops are validated separately and may not downgrade."""
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/|$)", url):
            url = "https://" + url
    try:
        parsed = httpx.URL(url)
        host = (parsed.host or "").rstrip(".").lower()
        if (parsed.scheme == "http"
                and any(host == root or host.endswith("." + root)
                        for root in _LINK_HOSTS)):
            return str(parsed.copy_with(scheme="https", port=None))
    except (TypeError, httpx.InvalidURL):
        pass
    return url


# Backward-compatible internal name used by older callers/tests.
_normalize_url = normalize_url


def resolve_link(url: str, run_counter: Optional[Callable[[], None]] = None,
                 retry_failed: bool = False) -> Resolution:
    """Resolve a PLOG POST LINK to a note id (plus detail when TikHub ran).

    Never raises — every failure becomes a Resolution(status='failed').
    Successful resolutions are cached permanently (a resolved note id never
    changes); failures are cached and retried only after a TTL or on request.
    """
    normalized_url = normalize_url(url)
    with _url_singleflight(normalized_url):
        try:
            # Cache is rechecked by the inner function only after this caller
            # owns the key, so concurrent runs cannot both observe a miss.
            return _resolve_link_inner(
                normalized_url, run_counter, retry_failed
            )
        except Exception as e:  # absolute backstop: never kill a run
            logger.exception("internal link resolver failure")
            return Resolution(
                status="failed",
                error=f"internal resolver error ({type(e).__name__})",
            )


def _resolve_link_inner(url: str, run_counter: Optional[Callable[[], None]],
                        retry_failed: bool) -> Resolution:
    url = normalize_url(url)
    if not url:
        return Resolution(status="failed", error="empty link")
    if not _allowed_link_url(url):
        return Resolution(
            status="failed",
            error=f"unsupported link host (expected Xiaohongshu): {url[:80]}",
        )

    cached = db.cache_get(url)
    if cached:
        if cached["status"] == "ok":
            return _res_from_cache(cached)
        age_h = (time.time() - cached["resolved_at"]) / 3600
        if not retry_failed and age_h < config.FAILED_CACHE_TTL_HOURS:
            return _res_from_cache(cached)

    # 1. Free first try: walk the redirect chain ourselves. When it works, the
    # note id alone is enough for the Tier-1 join — TikHub (which costs money)
    # is deferred to ensure_author, which the pipeline invokes only when the
    # join misses and the 无博主/无帖子 decision actually needs the author id.
    note_id = direct_resolve(url)
    if note_id:
        db.cache_put(url, status="ok", note_id=note_id, source="direct")
        return Resolution(status="ok", note_id=note_id, source="direct")

    # 2. Authoritative: TikHub accepts the raw share URL via share_text and
    # returns full note detail (note_id, author, engagement snapshot, title).
    try:
        raw_body = tikhub_fetch_note(share_url=url, counter=run_counter)
        fields = _extract_note_fields(raw_body)
    except TikHubError as e:
        logger.info("TikHub note lookup failed", exc_info=True)
        err = ("TIKHUB_API_KEY not configured"
               if "not configured" in str(e)
               else "TikHub note lookup failed")
        db.cache_put(url, status="failed", error=err[:500])
        return Resolution(status="failed", error=err)

    if not fields.get("note_id"):
        err = "TikHub responded but no note id could be extracted from the payload"
        db.cache_put(url, status="failed", error=err,
                     raw_json=json.dumps(raw_body, ensure_ascii=False)[:200_000])
        return Resolution(status="failed", error=err)

    res = Resolution(
        status="ok", note_id=fields["note_id"],
        author_id=fields.get("author_id", ""),
        author_name=fields.get("author_name", ""),
        likes=fields.get("likes"), collects=fields.get("collects"),
        comments=fields.get("comments"), title=fields.get("title", ""),
        publish_time=fields.get("publish_time", ""),
        source="tikhub", raw=raw_body,
    )
    db.cache_put(
        url, status="ok", note_id=res.note_id, author_id=res.author_id or None,
        author_name=res.author_name or None, likes=res.likes,
        collects=res.collects, comments=res.comments, title=res.title or None,
        publish_time=res.publish_time or None, source="tikhub",
        raw_json=json.dumps(raw_body, ensure_ascii=False)[:200_000],
    )
    return res


def ensure_author(url: str, res: Resolution,
                  run_counter: Optional[Callable[[], None]] = None,
                  retry_failed: bool = False) -> Resolution:
    if res.author_id or not res.ok:
        return res
    normalized_url = normalize_url(url)
    with _url_singleflight(normalized_url):
        cached = db.cache_get(normalized_url)
        if cached and cached.get("status") == "ok":
            cached_res = _res_from_cache(cached)
            if (cached_res.note_id.lower() == res.note_id.lower()
                    and cached_res.author_id):
                return cached_res
        return _ensure_author_inner(
            normalized_url, res, run_counter, retry_failed
        )


def _ensure_author_inner(url: str, res: Resolution,
                         run_counter: Optional[Callable[[], None]],
                         retry_failed: bool) -> Resolution:
    """Make sure *res* carries an author_id, fetching from TikHub by note_id
    when the fast path resolved the note without author detail. Author-fetch
    failures are cached with the same TTL as link failures so a permanently
    broken enrichment is not re-billed on every run. Never raises."""
    # Free first try: the note page's SSR state carries the author id. This
    # runs even when a previous enrichment failed (it costs nothing) — the
    # failure TTL below only gates the paid TikHub call.
    page = direct_fetch_note_detail(url, expected_note_id=res.note_id)
    if page.get("author_id"):
        res.author_id = page["author_id"]
        res.author_name = page.get("author_name") or res.author_name
        res.likes = page.get("likes") if page.get("likes") is not None else res.likes
        res.collects = page.get("collects") if page.get("collects") is not None else res.collects
        res.comments = page.get("comments") if page.get("comments") is not None else res.comments
        res.title = page.get("title") or res.title
        res.publish_time = page.get("publish_time") or res.publish_time
        res.source = res.source + "+page"
        res.error = ""
        db.cache_merge(
            url, author_id=res.author_id, author_name=res.author_name or None,
            likes=res.likes, collects=res.collects, comments=res.comments,
            title=res.title or None, publish_time=res.publish_time or None,
            source=res.source, error=None, author_failed_at=None,
            resolved_at=time.time(),
        )
        return res

    cached = db.cache_get(url) or {}
    failed_at = cached.get("author_failed_at")
    if failed_at and not retry_failed:
        if (time.time() - failed_at) / 3600 < config.FAILED_CACHE_TTL_HOURS:
            return res

    try:
        body = tikhub_fetch_note(note_id=res.note_id, counter=run_counter)
        fields = _extract_note_fields(body)
        returned_note_id = fields.get("note_id") or ""
        if returned_note_id != res.note_id.lower():
            res.error = "TikHub author response could not verify the requested note identity"
            db.cache_merge(url, author_failed_at=time.time(),
                           error=res.error)
        elif fields.get("author_id"):
            res.author_id = fields["author_id"]
            res.author_name = fields.get("author_name") or res.author_name
            res.likes = fields.get("likes") if fields.get("likes") is not None else res.likes
            res.collects = fields.get("collects") if fields.get("collects") is not None else res.collects
            res.comments = fields.get("comments") if fields.get("comments") is not None else res.comments
            res.title = fields.get("title") or res.title
            res.publish_time = fields.get("publish_time") or res.publish_time
            res.raw = body
            res.source = res.source if "tikhub" in res.source else res.source + "+tikhub"
            res.error = ""
            db.cache_merge(
                url, author_id=res.author_id, author_name=res.author_name or None,
                likes=res.likes, collects=res.collects, comments=res.comments,
                title=res.title or None, publish_time=res.publish_time or None,
                source=res.source, error=None, author_failed_at=None,
                resolved_at=time.time(),
                raw_json=json.dumps(body, ensure_ascii=False)[:200_000],
            )
        else:
            res.error = res.error or "TikHub payload carried no author id"
            db.cache_merge(url, author_failed_at=time.time(),
                           error=res.error[:500])
    except TikHubError as e:
        logger.info("TikHub author lookup failed", exc_info=True)
        public_error = ("TIKHUB_API_KEY not configured"
                        if "not configured" in str(e)
                        else "TikHub author lookup failed")
        res.error = res.error or public_error
        db.cache_merge(url, author_failed_at=time.time(), error=public_error)
    except Exception as e:  # same backstop as resolve_link
        logger.exception("internal author enrichment failure")
        res.error = res.error or f"internal enrichment error ({type(e).__name__})"
    return res
