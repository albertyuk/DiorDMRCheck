"""Name-matching policy — the single owner of the ladder.

The four-rung ladder (CJK substring → normalized substring → ASCII fuzzy →
pinyin bridge), its thresholds, and its method-name vocabulary are defined
here once. Two implementations consume it: ``name_ladder`` below for a
single PLOG/DMR pair (Tier 3), and ``perimeter.PerimeterIndex.scan_name``
for the batch scan over precomputed forms — a threshold tweak or a renamed
rung now changes both in lockstep instead of drifting.
"""
from __future__ import annotations

from pypinyin import lazy_pinyin
from rapidfuzz import fuzz

from ..core.textnorm import ascii_part, cjk, norm

# Ladder policy. Both sides of a fuzzy comparison need ≥ MIN_COMPARE_LEN
# chars: partial_ratio aligns the shorter string inside the longer, so a
# 1-3 char remainder scores 100 against anything.
FUZZY_CUTOFF = 85
MIN_COMPARE_LEN = 4

# Rung names — carried into Verdict evidence, reports, and the audit JSON.
METHOD_CJK = "cjk-substring"
METHOD_NORM = "norm-substring"
METHOD_ASCII_FUZZY = "ascii-fuzzy"
METHOD_PINYIN = "pinyin-bridge"


def name_contains(plog_name: str, dmr_blogger: str) -> bool:
    """Strict containment used for the MATCH name-mislabel nuance: the DMR
    Blogger must contain the (normalized) PLOG NAME. Fuzzy matching is
    deliberately NOT used here — the human flags e.g. gungun_ vs gungunnnnn."""
    c = cjk(plog_name)
    if c and c in cjk(dmr_blogger):
        return True
    n = norm(plog_name)
    if not n:
        # An all-emoji/blank PLOG name normalizes to nothing — there is no
        # basis to accuse DMR of mislabeling, so treat as containing.
        return True
    return n in norm(dmr_blogger)


def name_ladder(plog_name: str, dmr_blogger: str) -> str:
    """First-hit-wins ladder; returns the method name or '' for no match."""
    pc, dc = cjk(plog_name), cjk(dmr_blogger)
    if pc and pc in dc:
        return METHOD_CJK
    pn, dn = norm(plog_name), norm(dmr_blogger)
    if pn and pn in dn:
        return METHOD_NORM
    pa, da = ascii_part(plog_name), ascii_part(dmr_blogger)
    if (len(pa) >= MIN_COMPARE_LEN and len(da) >= MIN_COMPARE_LEN
            and fuzz.partial_ratio(pa, da) >= FUZZY_CUTOFF):
        return METHOD_ASCII_FUZZY
    if pc and len(da) >= MIN_COMPARE_LEN:
        pinyin = "".join(lazy_pinyin(pc)).casefold()
        if (len(pinyin) >= MIN_COMPARE_LEN
                and fuzz.partial_ratio(pinyin, da) >= FUZZY_CUTOFF):
            return METHOD_PINYIN
    return ""
