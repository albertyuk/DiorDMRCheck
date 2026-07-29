"""Reconciler domain vocabulary and records.

The single owner of the status constants, the human column-S vocabulary
(reproducing the reference file exactly), the Xiaohongshu note-id rule, and
the Verdict/Candidate records every subsystem exchanges. Algorithms live in
the matching pipeline; web/report/eval layers import names from here without
pulling in the pipeline's heavy dependencies.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# Statuses
MATCH = "MATCH"
NO_POST = "NO_POST"
NO_BLOGGER = "NO_BLOGGER"
LINK_ERROR = "LINK_ERROR"
REVIEW = "REVIEW"
# Perimeter-split variants of NO_BLOGGER (only when a perimeter is loaded):
# inside DMR's monitored Micro perimeter yet absent from the export → a
# genuine DMR gap, grouped with 无帖子 in summary buckets.
NO_POST_IN_PERIMETER = "NO_POST_IN_PERIMETER"
NO_BLOGGER_NOT_IN_PERIMETER = "NO_BLOGGER_NOT_IN_PERIMETER"

# Human vocabulary for column S (reproduces the reference file exactly)
S_TEXT = {
    MATCH: "",
    NO_BLOGGER: "无博主",
    NO_POST: "无帖子",
    LINK_ERROR: "Check链接错误",
    REVIEW: "人工复核",
    NO_POST_IN_PERIMETER: "无博主但在Perimeter内→无帖子",
    NO_BLOGGER_NOT_IN_PERIMETER: "无博主（不在Perimeter内）",
}
NAME_MISLABEL = "有 但是DMR博主名字标注错误"

ENGAGEMENT_CAVEAT = (
    "DMR engagement is a first-crawl snapshot (often within hours of posting) "
    "and is NOT comparable to KOL-tracker finals — shown as context only, never used "
    "for matching."
)

# Xiaohongshu note ids are 24-char hex strings (domain knowledge, not generic
# text normalization — hence here, next to the join semantics that rely on it).
HEX24 = re.compile(r"\b([0-9a-fA-F]{24})\b")


def is_hex24(s: str) -> bool:
    s = (s or "").strip()
    return len(s) == 24 and bool(re.fullmatch(r"[0-9a-fA-F]{24}", s))


@dataclass
class Candidate:
    dmr_row: int
    post_id: str
    blogger: str
    username: str
    post_date: Optional[str]
    date_delta_days: Optional[int]
    likes_retweet: Optional[int]
    name_method: str


@dataclass
class Verdict:
    campaign: str
    no: str
    name: str
    post_date: Optional[str]
    post_link: str
    excel_row: int
    status: str = REVIEW
    tier: str = ""
    name_mislabel: bool = False
    review_reason: str = ""
    # evidence
    resolved_note_id: str = ""
    resolved_author_id: str = ""
    resolved_author_name: str = ""
    resolution_source: str = ""
    resolution_error: str = ""
    matched_dmr_row: Optional[int] = None
    matched_post_id: str = ""
    matched_blogger: str = ""
    matched_username: str = ""
    matched_post_date: Optional[str] = None
    date_delta_days: Optional[int] = None
    name_method: str = ""
    plog_like: Optional[int] = None
    dmr_likes_retweet: Optional[int] = None
    # DMR engagement snapshot of the matched row (export "weighted engagement
    # data" section — context only, never a matching signal)
    dmr_share_favorites: Optional[int] = None
    dmr_comments: Optional[int] = None
    dmr_engagement: Optional[int] = None
    dmr_weighted_eng: Optional[float] = None
    # NOTE: the engagement caveat (ENGAGEMENT_CAVEAT) is document-level
    # context, emitted once per result/audit document — not stored per row.
    out_of_window: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Tier-4 adjudication
    llm_verdict: str = ""
    llm_confidence: Optional[float] = None
    llm_rationale_en: str = ""
    llm_rationale_zh: str = ""
    # Perimeter cross-check evidence
    # For rows that were initially 无博主 and had at least one perimeter list
    # checked: which list(s) the resolved author's REDBOOK_ID was found in —
    # "micro" | "macro" | "both" | "none". Empty for all other rows.
    perimeter_membership: str = ""
    perimeter_method: str = ""            # redbook-id | <name-ladder step> | ""
    perimeter_name: str = ""
    perimeter_namebis: str = ""
    perimeter_dmrid: str = ""
    perimeter_redbook_id: str = ""
    perimeter_followers: Optional[int] = None
    perimeter_extraction_date: str = ""
    perimeter_note: str = ""
    perimeter_candidates: list[str] = field(default_factory=list)

    def column_s(self) -> str:
        """Render the human-vocabulary annotation for column S."""
        if self.status == MATCH:
            return NAME_MISLABEL if self.name_mislabel else ""
        base = S_TEXT[self.status]
        if self.status in (NO_POST, NO_BLOGGER, NO_POST_IN_PERIMETER,
                           NO_BLOGGER_NOT_IN_PERIMETER) and self.out_of_window:
            # Expected-missing, not a DMR gap: warn, don't flag (§1b).
            return base + "（超出DMR导出窗口，预期缺失）"
        if self.status == LINK_ERROR and self.candidates:
            c = self.candidates[0]
            delta = f"Δ={c.date_delta_days}天" if c.date_delta_days is not None else "日期未知"
            base += f"（同名候选: {c.blogger} {c.post_id} {delta}）"
        elif self.status == LINK_ERROR and not self.candidates:
            base += "（无同名候选）"
        elif self.status == REVIEW and self.review_reason:
            base += f"（{self.review_reason}）"
        return base

    def to_dict(self) -> dict:
        d = asdict(self)
        d["column_s"] = self.column_s()
        return d
