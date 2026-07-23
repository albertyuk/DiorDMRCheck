"""Bonus tab — reverse audit.

DMR rows within the campaign date window whose Username is in the campaign's
resolved author-ID set but whose PostID matched no PLOG row: extra posts DMR
captured that PLOG doesn't track.
"""
from __future__ import annotations

from datetime import timedelta

from .domain import Verdict, is_hex24
from .pipeline import matched_post_ids
from .parsers import DmrParse, PlogParse


def reverse_audit(plog: PlogParse, dmr: DmrParse,
                  verdicts: list[Verdict]) -> list[dict]:
    matched = matched_post_ids(verdicts)
    represented = matched | {
        verdict.resolved_note_id.strip().lower()
        for verdict in verdicts
        if is_hex24(verdict.resolved_note_id)
    }

    campaign_windows: dict[str, tuple] = {}
    for campaign in dict.fromkeys(row.campaign for row in plog.rows):
        dates = [row.post_date for row in plog.rows
                 if row.campaign == campaign and row.post_date]
        if dates:
            campaign_windows[campaign] = (
                min(dates) - timedelta(days=1),
                max(dates) + timedelta(days=1),
            )

    author_campaigns: dict[str, set[str]] = {}
    for verdict in verdicts:
        author_id = (verdict.resolved_author_id or "").strip().lower()
        if author_id and verdict.campaign in campaign_windows:
            author_campaigns.setdefault(author_id, set()).add(verdict.campaign)

    out = []
    for r in dmr.rows:
        author_id = (r.username or "").strip().lower()
        if not author_id or author_id not in author_campaigns:
            continue
        # Without a stable post id or date the row cannot satisfy the reverse
        # audit contract (an unmatched post *inside* a campaign window).
        post_id = (r.post_id or "").strip().lower()
        if not post_id or not r.post_date or post_id in represented:
            continue
        campaigns = sorted(
            campaign for campaign in author_campaigns[author_id]
            if campaign_windows[campaign][0]
            <= r.post_date.date()
            <= campaign_windows[campaign][1]
        )
        if not campaigns:
            continue
        out.append({
            "campaigns": campaigns,
            "dmr_row": r.excel_row,
            "blogger": r.blogger,
            "username": r.username,
            "post_id": post_id,
            "post_date": r.post_date.strftime("%Y-%m-%d %H:%M") if r.post_date else None,
            "likes_retweet": r.likes_retweet,
            "engagement": r.engagement,
        })
    out.sort(key=lambda x: (x["campaigns"], x["blogger"], x["post_date"] or ""))
    return out
