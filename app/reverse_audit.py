"""Bonus tab — reverse audit.

DMR rows within the campaign date window whose Username is in the campaign's
resolved author-ID set but whose PostID matched no PLOG row: extra posts DMR
captured that PLOG doesn't track.
"""
from __future__ import annotations

from datetime import timedelta

from .matcher import Verdict, matched_post_ids, resolved_author_ids
from .parsers import DmrParse, PlogParse


def reverse_audit(plog: PlogParse, dmr: DmrParse,
                  verdicts: list[Verdict]) -> list[dict]:
    authors = resolved_author_ids(verdicts)
    matched = matched_post_ids(verdicts)
    d_from, d_to = plog.date_range
    if d_from:
        d_from -= timedelta(days=1)
    if d_to:
        d_to += timedelta(days=1)

    out = []
    for r in dmr.rows:
        if not r.username or r.username not in authors:
            continue
        if r.post_id in matched:
            continue
        if d_from and d_to and r.post_date and not (d_from <= r.post_date.date() <= d_to):
            continue
        out.append({
            "dmr_row": r.excel_row,
            "blogger": r.blogger,
            "username": r.username,
            "post_id": r.post_id,
            "post_date": r.post_date.strftime("%Y-%m-%d %H:%M") if r.post_date else None,
            "likes_retweet": r.likes_retweet,
            "engagement": r.engagement,
        })
    out.sort(key=lambda x: (x["blogger"], x["post_date"] or ""))
    return out
