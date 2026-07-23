"""The matching pipeline (Tiers 0–3).

Deterministic-first: the only signals allowed to assert MATCH / NO_POST /
NO_BLOGGER are the exact note-ID join (Tier 1) and the author-ID lookup
(Tier 2). Name heuristics (Tier 3) only rank candidates for human review,
and engagement comparison is never a decision signal — it is carried purely
as reviewer context, with a caveat.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
import logging
from typing import Callable, Optional

from .. import config
from ..core.textnorm import cjk, norm
from .name_match import (METHOD_CJK, METHOD_NORM, name_contains,
                         name_ladder)
from .parsers import DmrParse, DmrRow, PlogParse, PlogRow
from .domain import (ENGAGEMENT_CAVEAT, LINK_ERROR, MATCH,  # noqa: F401
                     NAME_MISLABEL, NO_BLOGGER,
                     NO_BLOGGER_NOT_IN_PERIMETER, NO_POST,
                     NO_POST_IN_PERIMETER, REVIEW, S_TEXT,
                     Candidate, Verdict)
from .links import Resolution, ensure_author, normalize_url, resolve_link

logger = logging.getLogger(__name__)


# Hard ceiling on Python-level name-ladder pair comparisons per run. Exact
# note/author joins never consume this budget. Once exhausted, ambiguous rows
# remain REVIEW/LINK_ERROR without suggestions instead of attempting a
# user-controlled PLOG×DMR Cartesian product.
MAX_NAME_SCAN_COMPARISONS = 2_000_000


@dataclass
class DmrIndexes:
    by_post_id: dict[str, DmrRow]
    by_author_id: dict[str, list[DmrRow]]
    duplicate_post_ids: dict[str, list[DmrRow]]
    username_present: bool
    username_complete: bool
    rows: list[DmrRow]


@dataclass
class NameScanBudget:
    rows: list[DmrRow]
    remaining: int = MAX_NAME_SCAN_COMPARISONS

    def __post_init__(self) -> None:
        self.cache: dict[str, tuple[list[tuple[DmrRow, str]], bool]] = {}

    def lookup(self, plog_name: str) -> tuple[list[tuple[DmrRow, str]], bool]:
        key = norm(plog_name)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        cost = len(self.rows)
        if cost > self.remaining:
            result = ([], False)
        else:
            self.remaining -= cost
            result = (scan_by_name(plog_name, self.rows), True)
        self.cache[key] = result
        return result


def build_indexes(dmr: DmrParse) -> DmrIndexes:
    by_post_id: dict[str, DmrRow] = {}
    by_author_id: dict[str, list[DmrRow]] = {}
    post_rows: dict[str, list[DmrRow]] = {}
    for r in dmr.rows:
        post_id = (r.post_id or "").strip().lower()
        author_id = (r.username or "").strip().lower()
        if post_id:
            post_rows.setdefault(post_id, []).append(r)
        if author_id:
            by_author_id.setdefault(author_id, []).append(r)
    duplicates = {post_id: rows for post_id, rows in post_rows.items()
                  if len(rows) > 1}
    by_post_id = {post_id: rows[0] for post_id, rows in post_rows.items()
                  if len(rows) == 1}
    return DmrIndexes(
        by_post_id=by_post_id,
        by_author_id=by_author_id,
        duplicate_post_ids=duplicates,
        username_present=bool(by_author_id),
        username_complete=bool(dmr.rows) and all(r.username for r in dmr.rows),
        rows=dmr.rows,
    )


# ------------------------------------------------------------- name matching

def scan_by_name(plog_name: str, rows: list[DmrRow]) -> list[tuple[DmrRow, str]]:
    hits = []
    for r in rows:
        method = name_ladder(plog_name, r.blogger)
        if method:
            hits.append((r, method))
    return hits


def _delta_days(plog_date: Optional[date], dmr_row: DmrRow) -> Optional[int]:
    if not plog_date or not dmr_row.post_date:
        return None
    return (dmr_row.post_date.date() - plog_date).days


def rank_candidates(prow: PlogRow, hits: list[tuple[DmrRow, str]],
                    keep_out_of_window: bool = False) -> list[Candidate]:
    """Rank name-matched DMR rows by date proximity within the ±window.
    Date is a soft ranking signal only — never a hard filter for the verdict,
    but candidates outside the window are dropped from the suggestion list
    (they are almost certainly different posts) unless the caller needs them
    as evidence for a conflict review."""
    cands = []
    for r, method in hits:
        delta = _delta_days(prow.post_date, r)
        if (not keep_out_of_window and delta is not None
                and abs(delta) > config.CANDIDATE_DATE_WINDOW_DAYS):
            continue
        cands.append(Candidate(
            dmr_row=r.excel_row, post_id=r.post_id, blogger=r.blogger,
            username=r.username,
            post_date=r.post_date.strftime("%Y-%m-%d") if r.post_date else None,
            date_delta_days=delta, likes_retweet=r.likes_retweet,
            name_method=method,
        ))
    cands.sort(key=lambda c: (c.date_delta_days is None,
                              abs(c.date_delta_days or 0)))
    return cands[:config.MAX_CANDIDATES_PER_VERDICT]


# -------------------------------------------------------- perimeter split

def _fill_perimeter_evidence(v: Verdict, row: dict, method: str) -> None:
    v.perimeter_method = method
    v.perimeter_name = row.get("name", "")
    v.perimeter_namebis = row.get("namebis", "")
    v.perimeter_dmrid = row.get("dmrid", "")
    v.perimeter_redbook_id = row.get("redbook_id", "")
    v.perimeter_followers = row.get("redbook_followers")


def apply_perimeter(v: Verdict, prow: PlogRow, perim) -> None:
    """Split NO_BLOGGER by Micro-perimeter membership (offline join; no new
    external calls). Only the REDBOOK_ID join may flip the verdict to
    'in-perimeter'; name hits are suggestions, recorded as evidence — name
    collisions are real, so ≥2 hits never auto-classify. LINK_ERROR rows get
    the name fallback as annotation only; their verdict never changes."""
    if perim is None:
        return
    v.perimeter_extraction_date = perim.extraction_date

    if v.status == NO_BLOGGER:
        row = perim.lookup_author(v.resolved_author_id)
        if row is not None:
            v.status = NO_POST_IN_PERIMETER
            _fill_perimeter_evidence(v, row, "redbook-id")
            v.notes.append(
                "Blogger is inside DMR's monitored Micro perimeter "
                f"(REDBOOK_ID {row['redbook_id']}) yet absent from the export "
                "— a genuine DMR gap, grouped with 无帖子."
            )
            return

        hits = perim.scan_name(prow.name)
        v.status = NO_BLOGGER_NOT_IN_PERIMETER
        if perim.last_scan_skipped:
            v.perimeter_note = (
                "Perimeter name suggestions omitted after the per-run safety "
                "budget was exhausted; REDBOOK_ID non-membership is unchanged."
            )
            v.notes.append(v.perimeter_note)
            return
        if len(hits) == 1:
            row, method = hits[0]
            _fill_perimeter_evidence(v, row, method)
            if row.get("redbook_id"):
                # same name, different XHS account — never classify by name
                v.perimeter_note = (
                    "同名Perimeter条目但REDBOOK_ID不同（近似未命中）/ same-name "
                    "perimeter entry carries a different REDBOOK_ID "
                    f"({row['redbook_id']} vs resolved {v.resolved_author_id or '?'})"
                )
            else:
                v.perimeter_note = (
                    "在Perimeter名单但未登记REDBOOK_ID — register the ID; DMR "
                    "cannot crawl an unregistered account"
                )
            v.notes.append(v.perimeter_note)
        elif len(hits) >= 2:
            v.perimeter_candidates = [
                f"{r.get('name') or r.get('namebis')} "
                f"[{r.get('redbook_id') or 'no REDBOOK_ID'}] ({m})"
                for r, m in hits[:8]
            ]
            v.perimeter_note = (
                f"{len(hits)}个同名Perimeter条目，无法按名字判定 / name matches "
                "multiple perimeter rows — never auto-picked by name"
            )
            v.notes.append(v.perimeter_note)
        return

    if v.status == LINK_ERROR:
        hits = perim.scan_name(prow.name)
        if perim.last_scan_skipped:
            v.perimeter_note = (
                "Perimeter name suggestions omitted after the per-run safety "
                "budget was exhausted; link-error verdict unchanged."
            )
            v.notes.append(v.perimeter_note)
            return
        if hits:
            v.perimeter_candidates = [
                f"{r.get('name') or r.get('namebis')} "
                f"[{r.get('redbook_id') or 'no REDBOOK_ID'}] ({m})"
                for r, m in hits[:8]
            ]
            v.perimeter_note = (
                "链接失效，仅作参考：名字命中Perimeter条目 / dead link — perimeter "
                "name hits recorded as evidence only, verdict unchanged"
            )
            v.notes.append(v.perimeter_note)


# ------------------------------------------------------------------ pipeline

ProgressCb = Callable[[str, int, int, str], None]


def _fill_match_evidence(v: Verdict, prow: PlogRow, drow: DmrRow) -> None:
    v.matched_dmr_row = drow.excel_row
    v.matched_post_id = drow.post_id
    v.matched_blogger = drow.blogger
    v.matched_username = drow.username
    v.matched_post_date = drow.post_date.strftime("%Y-%m-%d %H:%M") if drow.post_date else None
    v.date_delta_days = _delta_days(prow.post_date, drow)
    v.dmr_likes_retweet = drow.likes_retweet


def match_row(prow: PlogRow, idx: DmrIndexes, res: Resolution,
              window: tuple[Optional[date], Optional[date]],
              name_scans: Optional[NameScanBudget] = None
              ) -> Verdict:
    """Tiers 1–3 for one PLOG row, given its attempted link resolution.

    Display names are never promoted to account identity. They are mutable and
    non-unique, so only a note-id join or this row's resolved author id may
    produce a deterministic verdict.
    """
    v = Verdict(
        campaign=prow.campaign, no=prow.no, name=prow.name,
        post_date=prow.post_date.isoformat() if prow.post_date else None,
        post_link=prow.post_link, excel_row=prow.excel_row,
        plog_like=prow.like,
    )
    wf, wt = window
    if prow.post_date and wf and wt and not (wf <= prow.post_date <= wt):
        v.out_of_window = True
        v.notes.append(
            f"PLOG POST DATE {prow.post_date} is outside the DMR export window "
            f"{wf}..{wt} — an absent post is expected-missing, not a DMR gap."
        )

    note_id = (res.note_id or "").strip().lower()
    author_id = (res.author_id or "").strip().lower()
    v.resolved_note_id = note_id
    v.resolved_author_id = author_id
    v.resolved_author_name = res.author_name
    v.resolution_source = res.source
    v.resolution_error = res.error

    name_lookup: Optional[tuple[list[tuple[DmrRow, str]], bool]] = None

    def name_hits() -> list[tuple[DmrRow, str]]:
        nonlocal name_lookup
        if name_lookup is None:
            name_lookup = (name_scans.lookup(prow.name) if name_scans
                           else (scan_by_name(prow.name, idx.rows), True))
            if not name_lookup[1]:
                v.notes.append(
                    "Name-candidate scan skipped after the per-run safety "
                    "budget was exhausted; deterministic ID evidence is "
                    "unchanged, but no heuristic suggestions are shown."
                )
        return name_lookup[0]

    # ---- Tier 1: exact post match via resolved note id
    if res.ok:
        duplicate_rows = idx.duplicate_post_ids.get(note_id)
        if duplicate_rows:
            v.status = REVIEW
            v.tier = "1:duplicate-post-id"
            v.review_reason = (
                "DMR包含重复PostID / DMR contains duplicate rows for the "
                "resolved note id; no row was selected automatically"
            )
            v.candidates = rank_candidates(
                prow, [(row, "duplicate-post-id") for row in duplicate_rows],
                keep_out_of_window=True,
            )
            return v

        drow = idx.by_post_id.get(note_id)
        if drow is not None:
            v.status = MATCH
            v.tier = "1:note-id-join"
            _fill_match_evidence(v, prow, drow)
            if not name_contains(prow.name, drow.blogger):
                v.name_mislabel = True
                v.notes.append(
                    f"Note-ID join is certain, but DMR records the blogger as "
                    f"{drow.blogger!r} which does not contain PLOG name {prow.name!r}."
                )
            return v

        # ---- Tier 2: blogger presence via author id
        if author_id and not idx.username_present:
            # The DMR file has no usable Username column at all — a blanket
            # 无博主 for every row would be a schema artifact, not a finding.
            v.status = REVIEW
            v.tier = "2:no-username-column"
            v.review_reason = (
                "DMR缺少Username列，无法判定无博主/无帖子 / DMR has no usable "
                "Username column; blogger presence cannot be decided"
            )
            v.candidates = rank_candidates(prow, name_hits())
            return v
        if author_id:
            author_rows = idx.by_author_id.get(author_id)
            if author_rows:
                v.status = NO_POST
                v.tier = "2:author-id"
                v.notes.append(
                    f"DMR tracks author {author_id} "
                    f"({author_rows[0].blogger!r}, {len(author_rows)} post(s)) but "
                    f"this note {note_id} is not among them."
                )
                # Cross-check: name scan should not point at a different author.
                foreign = [
                    (r, m) for r, m in name_hits()
                    if r.username and r.username != author_id
                    and ((m == METHOD_CJK and len(cjk(prow.name)) >= 2)
                         or (m == METHOD_NORM and len(norm(prow.name)) >= 4))
                ]
                if foreign and not any(r.username == author_id
                                       for r, _ in name_hits()):
                    v.status = REVIEW
                    v.tier = "2:author-id+name-conflict"
                    v.review_reason = (
                        "作者ID在DMR中存在但名字匹配指向另一位博主 / author-id says the "
                        "blogger is tracked, yet the name ladder only matches a "
                        "different Username"
                    )
                v.candidates = rank_candidates(
                    prow, name_hits() or
                    [(r, "same-author") for r in author_rows]
                )
                return v
            # author absent from DMR
            if not idx.username_complete:
                v.status = REVIEW
                v.tier = "2:partial-username-column"
                v.review_reason = (
                    "DMR部分行缺少Username，无法可靠判定无博主 / DMR Username "
                    "index is incomplete, so absence from it cannot prove the "
                    "blogger is untracked"
                )
                v.candidates = rank_candidates(prow, name_hits())
                return v
            v.status = NO_BLOGGER
            v.tier = "2:author-id"
            # Cross-check: high-precision name hit contradicts "no blogger".
            # Length floors keep 1-char CJK / short norm names from flipping
            # correct verdicts to REVIEW via coincidental substrings.
            strong = [(r, m) for r, m in name_hits()
                      if (m == METHOD_CJK and len(cjk(prow.name)) >= 2)
                      or (m == METHOD_NORM and len(norm(prow.name)) >= 4)]
            if strong:
                v.status = REVIEW
                v.tier = "2:author-id+name-conflict"
                v.review_reason = (
                    "作者ID不在DMR但存在同名博主 / resolved author id is absent from "
                    "DMR, yet a same-name Blogger row exists — verify manually"
                )
                v.candidates = rank_candidates(prow, strong)
                if not v.candidates:
                    # all hits fell outside the ±window — still show them, the
                    # reviewer needs to see what the conflict is about
                    v.candidates = rank_candidates(prow, strong,
                                                   keep_out_of_window=True)
            return v

        # Resolved the note but could not obtain the author id (TikHub
        # unavailable). Only Tier 1/2 may assert; degrade to REVIEW.
        v.status = REVIEW
        v.tier = "1:resolved-no-author"
        v.review_reason = (
            "链接已解析但无法获取作者ID（TikHub不可用）/ note resolved but author id "
            "unavailable, cannot decide 无博主 vs 无帖子"
        )
        v.candidates = rank_candidates(prow, name_hits())
        return v

    # ---- Link never resolved → LINK_ERROR + Tier 3 candidates
    v.status = LINK_ERROR
    v.tier = "3:name-heuristic"
    v.candidates = rank_candidates(prow, name_hits())
    if v.candidates:
        c = v.candidates[0]
        v.name_method = c.name_method
        v.notes.append(
            "Link dead/unresolvable, so the note id is unverifiable — Tier 3 "
            "only ranks same-name candidates; it never asserts a match. "
            f"Best candidate: {c.blogger} ({c.post_id}) Δ={c.date_delta_days} days."
        )
    else:
        v.notes.append("Link dead/unresolvable and no name-based candidate either.")
    return v


def run_pipeline(plog: PlogParse, dmr: DmrParse,
                 progress: Optional[ProgressCb] = None,
                 tikhub_counter: Optional[Callable[[], None]] = None,
                 retry_failed_links: bool = False,
                 perimeter=None) -> list[Verdict]:
    """Resolve links (concurrently, bounded) then match every PLOG row.
    *perimeter* is an optional PerimeterIndex — when present, NO_BLOGGER rows
    are split by Micro-perimeter membership right after Tier 2."""
    idx = build_indexes(dmr)
    window = (dmr.window_from, dmr.window_to)
    total = len(plog.rows)

    def report(phase: str, done: int, msg: str) -> None:
        if progress:
            progress(phase, done, total, msg)

    # Phase 1: resolve links (cache-first).
    resolutions: dict[int, Resolution] = {}
    done = 0
    report("resolve", 0, f"Resolving links 0/{total}…")

    grouped: dict[str, list[tuple[int, PlogRow]]] = {}
    for i, prow in enumerate(plog.rows):
        grouped.setdefault(normalize_url(prow.post_link), []).append((i, prow))

    def _resolve(item: tuple[str, list[tuple[int, PlogRow]]]
                 ) -> tuple[list[int], Resolution]:
        _url_key, rows = item
        prow = rows[0][1]
        res = resolve_link(prow.post_link, run_counter=tikhub_counter,
                           retry_failed=retry_failed_links)
        # Fetch author detail only when the note-id join is going to miss —
        # this is the money case (无帖子 vs 无博主) and needs TikHub once.
        resolved_note_id = (res.note_id or "").strip().lower()
        if (res.ok and resolved_note_id not in idx.by_post_id
                and resolved_note_id not in idx.duplicate_post_ids):
            res = ensure_author(prow.post_link, res, run_counter=tikhub_counter,
                                retry_failed=retry_failed_links)
        return [i for i, _ in rows], res

    with ThreadPoolExecutor(max_workers=config.TIKHUB_CONCURRENCY) as pool:
        for indexes, res in pool.map(_resolve, grouped.items()):
            for i in indexes:
                resolutions[i] = res
            done += len(indexes)
            report("resolve", done, f"Resolving links {done}/{total}…")

    # Phase 2: tiered matching (fast, in order). A bug on one row must not
    # take down the run — degrade that row to REVIEW with the error attached.
    verdicts: list[Verdict] = []
    name_scans = NameScanBudget(
        idx.rows, remaining=MAX_NAME_SCAN_COMPARISONS
    )
    for i, prow in enumerate(plog.rows):
        try:
            v = match_row(prow, idx, resolutions[i], window,
                          name_scans=name_scans)
            apply_perimeter(v, prow, perimeter)
            verdicts.append(v)
        except Exception as e:
            logger.exception(
                "matching failed for campaign=%r row=%s",
                prow.campaign, prow.excel_row,
            )
            v = Verdict(
                campaign=prow.campaign, no=prow.no, name=prow.name,
                post_date=prow.post_date.isoformat() if prow.post_date else None,
                post_link=prow.post_link, excel_row=prow.excel_row,
                status=REVIEW, tier="error",
                review_reason=f"内部错误 / internal error: {type(e).__name__}",
            )
            v.notes.append(
                "An internal matching error was recorded in server logs; "
                "this row was not classified automatically."
            )
            verdicts.append(v)
        report("match", i + 1, f"Matching rows {i + 1}/{total}…")
    return verdicts


def matched_post_ids(verdicts: list[Verdict]) -> set[str]:
    return {
        v.matched_post_id.strip().lower()
        for v in verdicts
        if v.matched_post_id and v.matched_post_id.strip()
    }


def resolved_author_ids(verdicts: list[Verdict]) -> set[str]:
    return {
        v.resolved_author_id.strip().lower()
        for v in verdicts
        if v.resolved_author_id and v.resolved_author_id.strip()
    }


def status_counts(verdicts: list[Verdict] | list[dict]) -> dict[str, int]:
    def value(v, key, default=None):
        return v.get(key, default) if isinstance(v, dict) else getattr(v, key, default)

    counts: dict[str, int] = {}
    for v in verdicts:
        status = value(v, "status", REVIEW)
        counts[status] = counts.get(status, 0) + 1
    if any(value(v, "name_mislabel", False) for v in verdicts):
        counts["MATCH_name_mislabel"] = sum(
            1 for v in verdicts if value(v, "name_mislabel", False)
        )
    return counts


def summary_buckets(verdicts: list[Verdict] | list[dict]) -> dict[str, int]:
    """The actionable grouping when a perimeter is in play: genuine DMR gaps
    (missed posts + in-perimeter absent bloggers) vs out-of-scope bloggers."""
    if isinstance(verdicts, dict):
        raise TypeError(
            "summary_buckets requires row verdicts so out-of-window rows can "
            "be excluded; aggregate counts are insufficient"
        )
    def value(v, key, default=None):
        return v.get(key, default) if isinstance(v, dict) else getattr(v, key, default)

    return {
        "dmr_gaps": sum(
            value(v, "status") in {NO_POST, NO_POST_IN_PERIMETER}
            and not value(v, "out_of_window", False)
            for v in verdicts
        ),
        "expected_missing": sum(
            value(v, "status") in {
                NO_POST, NO_BLOGGER, NO_POST_IN_PERIMETER,
                NO_BLOGGER_NOT_IN_PERIMETER,
            } and value(v, "out_of_window", False)
            for v in verdicts
        ),
        "outside_perimeter": sum(
            value(v, "status") == NO_BLOGGER_NOT_IN_PERIMETER
            and not value(v, "out_of_window", False)
            for v in verdicts
        ),
    }
