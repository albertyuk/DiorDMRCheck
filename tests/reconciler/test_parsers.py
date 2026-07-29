from datetime import date

from app.reconciler.parsers import parse_dmr, parse_plog
from tests import fixtures


def test_plog_header_fingerprint(plog_path):
    p = parse_plog(plog_path)
    assert p.sheet == "MASTER KOL LIST"
    assert p.header_row == 1
    # quirky headers mapped despite full-width paren / double space
    assert "fanbase(k)" in p.columns
    assert "ttlengagement" in p.columns


def test_plog_rows_and_sections(plog_path):
    p = parse_plog(plog_path)
    assert len(p.rows) == len(fixtures.plog_rows())
    # original order preserved: campaign #002 first, then #001, then #003
    assert p.campaigns == ["PLOG #002", "PLOG #001", "PLOG #003"]
    # NO resets per campaign → identity is (CAMPAIGN, NO)
    keys = [r.key for r in p.rows]
    assert ("PLOG #002", "1") in keys and ("PLOG #001", "1") in keys
    assert len(set(keys)) == len(keys)


def test_plog_emoji_name_preserved(plog_path):
    p = parse_plog(plog_path)
    assert any(r.name == "一颗鸡蛋🥚" for r in p.rows)


def test_plog_dates_parsed(plog_path):
    p = parse_plog(plog_path)
    row = next(r for r in p.rows if r.name == "饼饼")
    assert row.post_date == date(2026, 7, 1)


def test_dmr_header_below_metadata(dmr_path):
    d = parse_dmr(dmr_path)
    assert d.sheet == "Streaming"
    assert d.header_row == 3  # two metadata rows above


def test_dmr_window_parsed(dmr_path):
    d = parse_dmr(dmr_path)
    assert d.window_from == date(2026, 1, 1)
    assert d.window_to == date(2026, 7, 20)


def test_dmr_link_hyperlink_extraction(dmr_path):
    d = parse_dmr(dmr_path)
    r = d.rows[0]
    assert r.link_target.startswith("https://www.dmr.st/redi.html?url=")
    # embedded PostID agrees with the PostID column → no warning for it
    assert r.link_embedded_post_id == r.post_id


def test_dmr_rows_complete(dmr_path):
    d = parse_dmr(dmr_path)
    assert len(d.rows) == len(fixtures.dmr_rows())
    early = next(r for r in d.rows if r.post_id == fixtures.N_MOCHI_JUN)
    assert early.likes_retweet == 14  # first-crawl snapshot kept verbatim


def test_dmr_weighted_engagement_parsed(dmr_path):
    """The "WEIGHTED ENG." column is copied verbatim into the export's
    weighted-engagement-data section, so the parser must pick it up."""
    d = parse_dmr(dmr_path)
    early = next(r for r in d.rows if r.post_id == fixtures.N_MOCHI_JUN)
    assert early.weighted_eng == 14.5  # fixture writes likes + 0.5
    assert early.share_favorites == 1 and early.engagement == 15
    assert early.comments == 0
