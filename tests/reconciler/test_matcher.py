"""End-to-end pipeline tests over the synthetic fixtures — every §8 edge case
that has deterministic behavior is asserted here, with the network faked."""
from __future__ import annotations

import pytest

from app.reconciler.pipeline import (LINK_ERROR, MATCH, NAME_MISLABEL, NO_BLOGGER, NO_POST,
                         REVIEW, run_pipeline)
from app.reconciler.parsers import parse_dmr, parse_plog
from app.reconciler.reverse_audit import reverse_audit
from tests import fixtures


@pytest.fixture
def verdicts(plog_path, dmr_path, fake_resolver):
    plog = parse_plog(plog_path)
    dmr = parse_dmr(dmr_path)
    vs = run_pipeline(plog, dmr)
    return {(v.campaign, v.no): v for v in vs}, plog, dmr, vs


def test_same_blogger_different_nearby_post_is_no_post(verdicts):
    """墨池墨吟 PLOG 05-13 vs DMR 05-11 note — only the note-ID join can prove
    无帖子; the Δ2d same-name post must NOT be treated as the same post."""
    by, *_ = verdicts
    v = by[("PLOG #002", "1")]
    assert v.status == NO_POST
    assert v.tier == "2:author-id"
    assert v.column_s() == "无帖子"


def test_early_crawl_snapshot_does_not_affect_match(verdicts):
    """Verified same-post pair reads 607 PLOG likes vs 14 DMR likes — the
    engagement gulf must not weaken the note-ID MATCH."""
    by, *_ = verdicts
    v = by[("PLOG #001", "1")]
    assert v.status == MATCH
    assert v.column_s() == ""
    assert v.dmr_likes_retweet == 14 and v.plog_like == 607


def test_date_drift_match(verdicts):
    """饼饼 07-01 vs DMR PostDate 07-05 — Δ4d must not break the match."""
    by, *_ = verdicts
    v = by[("PLOG #001", "2")]
    assert v.status == MATCH
    assert v.date_delta_days == 4


def test_name_mislabel_nuance(verdicts):
    """gungun_ recorded by DMR as gungunnnnn → matched, but annotated with the
    human's mislabel vocabulary."""
    by, *_ = verdicts
    v = by[("PLOG #001", "3")]
    assert v.status == MATCH
    assert v.name_mislabel
    assert v.column_s() == NAME_MISLABEL


def test_dead_link_with_real_counterpart(verdicts):
    """鸡腿子 — human flagged the link, yet a same-name DMR post exists.
    LINK_ERROR + ranked candidate, never an automatic MATCH."""
    by, *_ = verdicts
    v = by[("PLOG #001", "4")]
    assert v.status == LINK_ERROR
    assert v.candidates, "tier 3 must attach the same-name candidate"
    assert v.candidates[0].post_id == fixtures.N_JITUI
    s = v.column_s()
    assert s.startswith("Check链接错误")
    assert fixtures.N_JITUI in s


def test_blogger_absent_is_no_blogger(verdicts):
    by, *_ = verdicts
    v = by[("PLOG #002", "2")]  # 一颗鸡蛋🥚
    assert v.status == NO_BLOGGER
    assert v.column_s() == "无博主"


def test_author_conflict_goes_to_review(verdicts):
    """Resolved author id absent from DMR, but an exact same-name Blogger row
    exists under a different Username → REVIEW, not a silent verdict."""
    by, *_ = verdicts
    v = by[("PLOG #001", "5")]  # 早春的树
    assert v.status == REVIEW
    assert "name-conflict" in v.tier
    assert v.column_s().startswith("人工复核")


def test_out_of_window_warns_but_does_not_flag(verdicts):
    by, *_ = verdicts
    v = by[("PLOG #001", "6")]  # 冬日限定, 2025-12-01
    assert v.out_of_window
    assert "预期缺失" in v.column_s()


def test_duplicate_blogger_across_campaigns(verdicts):
    """Same author in PLOG #002/#001 and #003 with different posts — each row
    matches independently on its own note id."""
    by, *_ = verdicts
    v3 = by[("PLOG #003", "1")]
    assert v3.status == MATCH
    assert v3.matched_post_id == fixtures.N_DUP_C3


def test_sibling_author_inference(verdicts):
    """A note that resolves but whose detail is dead/blocked still gets a
    deterministic Tier-2 verdict when a sibling row of the same blogger
    established the author id."""
    by, *_ = verdicts
    v = by[("PLOG #003", "2")]
    assert v.status == NO_POST
    assert v.tier == "2:author-id-sibling"
    assert v.resolved_author_id == fixtures.U_MOCHI
    assert v.column_s() == "无帖子"


def test_reverse_audit_finds_untracked_post(verdicts):
    by, plog, dmr, vs = verdicts
    rows = reverse_audit(plog, dmr, vs)
    pids = {r["post_id"] for r in rows}
    assert fixtures.N_EXTRA in pids
    # matched posts must never be listed as "extra"
    assert fixtures.N_MOCHI_JUN not in pids
    # the 无帖子 near-miss May post was never matched → it IS an extra post
    assert fixtures.N_MOCHI_MAY_DMR in pids


def test_no_row_crashes_and_all_rows_have_verdicts(verdicts):
    by, plog, _, vs = verdicts
    assert len(vs) == len(plog.rows)
    for v in vs:
        assert v.status in (MATCH, NO_POST, NO_BLOGGER, LINK_ERROR, REVIEW)
        v.column_s()  # must never raise


def test_only_deterministic_tiers_assert(verdicts):
    """Tier 3 may never emit MATCH / NO_POST / NO_BLOGGER."""
    _, _, _, vs = verdicts
    for v in vs:
        if v.tier.startswith("3:"):
            assert v.status in (LINK_ERROR, REVIEW)
