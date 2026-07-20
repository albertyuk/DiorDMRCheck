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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from . import config, db
from .normalize import HEX24, is_hex24

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# Global TikHub throttle: ≤ TIKHUB_CONCURRENCY requests in flight at once.
_tikhub_semaphore = threading.BoundedSemaphore(config.TIKHUB_CONCURRENCY)


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

def direct_resolve(url: str) -> Optional[str]:
    """Walk the redirect chain manually; return the note id or None."""
    for attempt in range(config.DIRECT_HTTP_RETRIES + 1):
        try:
            current = url
            with httpx.Client(
                follow_redirects=False,
                timeout=config.DIRECT_HTTP_TIMEOUT,
                headers={"User-Agent": MOBILE_UA},
            ) as client:
                for _hop in range(6):
                    m = HEX24.search(current)
                    if m and "xiaohongshu.com" in current:
                        return m.group(1).lower()
                    resp = client.get(current)
                    loc = resp.headers.get("location")
                    if resp.status_code in (301, 302, 303, 307, 308) and loc:
                        current = httpx.URL(current).join(loc).__str__()
                        continue
                    # Landed. One last look at the final URL.
                    m = HEX24.search(str(resp.url))
                    if m and "xiaohongshu.com" in str(resp.url):
                        return m.group(1).lower()
                    return None
            return None
        except httpx.HTTPError:
            if attempt < config.DIRECT_HTTP_RETRIES:
                time.sleep(0.5 * (attempt + 1))
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
    note_id = _walk(payload, ("note_id", "noteId", "id"))
    if not (isinstance(note_id, str) and is_hex24(note_id)):
        note_id = None
        m = HEX24.search(json.dumps(payload)[:20000])
        if m:
            note_id = m.group(1)
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
    pass


def _tikhub_get(path: str, params: dict, counter: Optional[Callable[[], None]]) -> dict:
    if not config.TIKHUB_API_KEY:
        raise TikHubError("TIKHUB_API_KEY not configured")
    url = config.TIKHUB_BASE_URL.rstrip("/") + path
    last_err: Exception = TikHubError("no attempt made")
    for attempt in range(config.TIKHUB_MAX_RETRIES + 1):
        try:
            with _tikhub_semaphore:
                if counter:
                    counter()
                with httpx.Client(timeout=config.TIKHUB_TIMEOUT) as client:
                    resp = client.get(
                        url, params=params,
                        headers={"Authorization": f"Bearer {config.TIKHUB_API_KEY}"},
                    )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                time.sleep(min(retry_after, 30))
                last_err = TikHubError("rate limited (429)")
                continue
            if 400 <= resp.status_code < 500:
                raise TikHubError(f"TikHub {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            body = resp.json()
            code = body.get("code")
            if code is not None and code not in (200, 0):
                raise TikHubError(f"TikHub code {code}: {str(body)[:300]}")
            return body
        except TikHubError as e:
            if "429" in str(e):
                last_err = e
                continue
            raise
        except (httpx.HTTPError, ValueError) as e:
            last_err = e
            if attempt < config.TIKHUB_MAX_RETRIES:
                time.sleep(2 ** attempt)
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

def resolve_link(url: str, run_counter: Optional[Callable[[], None]] = None,
                 retry_failed: bool = False) -> Resolution:
    """Resolve a PLOG POST LINK to note detail. Never raises."""
    url = (url or "").strip()
    if not url:
        return Resolution(status="failed", error="empty link")
    if not url.lower().startswith(("http://", "https://")):
        return Resolution(status="failed", error=f"not a URL: {url[:80]}")

    cached = db.cache_get(url)
    if cached:
        if cached["status"] == "ok":
            return _res_from_cache(cached)
        age_h = (time.time() - cached["resolved_at"]) / 3600
        if not retry_failed and age_h < config.FAILED_CACHE_TTL_HOURS:
            return _res_from_cache(cached)

    # 1. Free first try: walk redirects ourselves.
    note_id = direct_resolve(url)
    source = "direct" if note_id else ""

    # 2. Authoritative: TikHub (also fills author/engagement detail).
    fields: dict = {}
    raw_body: dict = {}
    tik_error = ""
    try:
        raw_body = tikhub_fetch_note(share_url=url, note_id=note_id or "",
                                     counter=run_counter)
        fields = _extract_note_fields(raw_body)
        if fields.get("note_id"):
            source = (source + "+tikhub") if source else "tikhub"
    except TikHubError as e:
        tik_error = str(e)

    final_note = fields.get("note_id") or note_id or ""
    if final_note:
        res = Resolution(
            status="ok", note_id=final_note,
            author_id=fields.get("author_id", ""),
            author_name=fields.get("author_name", ""),
            likes=fields.get("likes"), collects=fields.get("collects"),
            comments=fields.get("comments"), title=fields.get("title", ""),
            publish_time=fields.get("publish_time", ""),
            source=source or "direct",
            error=tik_error, raw=raw_body,
        )
        db.cache_put(
            url, status="ok", note_id=res.note_id, author_id=res.author_id or None,
            author_name=res.author_name or None, likes=res.likes,
            collects=res.collects, comments=res.comments, title=res.title or None,
            publish_time=res.publish_time or None, source=res.source,
            error=tik_error or None,
            raw_json=json.dumps(raw_body, ensure_ascii=False)[:200_000] if raw_body else None,
        )
        return res

    err = tik_error or "redirect chain did not reach a xiaohongshu note URL"
    db.cache_put(url, status="failed", error=err[:500], source=source or None)
    return Resolution(status="failed", error=err, source=source)


def ensure_author(url: str, res: Resolution,
                  run_counter: Optional[Callable[[], None]] = None) -> Resolution:
    """Make sure *res* carries an author_id, fetching from TikHub by note_id
    if the fast path resolved the note without author detail."""
    if res.author_id or not res.ok:
        return res
    try:
        body = tikhub_fetch_note(note_id=res.note_id, counter=run_counter)
        fields = _extract_note_fields(body)
        if fields.get("author_id"):
            res.author_id = fields["author_id"]
            res.author_name = fields.get("author_name") or res.author_name
            res.likes = fields.get("likes") if fields.get("likes") is not None else res.likes
            res.collects = fields.get("collects") if fields.get("collects") is not None else res.collects
            res.comments = fields.get("comments") if fields.get("comments") is not None else res.comments
            res.title = fields.get("title") or res.title
            res.publish_time = fields.get("publish_time") or res.publish_time
            res.raw = body
            res.source = (res.source + "+tikhub") if "tikhub" not in res.source else res.source
            db.cache_merge(
                url, author_id=res.author_id, author_name=res.author_name or None,
                likes=res.likes, collects=res.collects, comments=res.comments,
                title=res.title or None, publish_time=res.publish_time or None,
                source=res.source,
                raw_json=json.dumps(body, ensure_ascii=False)[:200_000],
            )
    except TikHubError as e:
        res.error = res.error or str(e)
    return res
