"""Tier 0 — text normalization.

norm(s): NFKC → strip emoji/symbol/format chars (So/Sk/Cf/Cs/Co plus emoji
ranges and variation selectors) → remove all whitespace → casefold.

cjk(s): concatenated CJK-ideograph runs of the NFKC'd string.
ascii_part(s): the non-CJK remainder of norm(s), stripped of trailing
``_ - .`` punctuation (so ``gungun_`` compares as ``gungun``).
"""
from __future__ import annotations

import re
import unicodedata

_CJK_RUN = re.compile("[\\u4e00-\\u9fff]+")
_WS = re.compile(r"\s+")

# Codepoint ranges that unicodedata categories miss for emoji purposes.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # emoji, symbols, pictographs
    (0xFE00, 0xFE0F),    # variation selectors (VS-16 is category Mn)
    (0x2600, 0x27BF),    # misc symbols + dingbats
    (0x2B00, 0x2BFF),
    (0x1F1E6, 0x1F1FF),  # regional indicators
)
_STRIP_CATEGORIES = {"So", "Sk", "Cf", "Cs", "Co"}


def _is_stripped(ch: str) -> bool:
    cp = ord(ch)
    if unicodedata.category(ch) in _STRIP_CATEGORIES:
        return True
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return cp == 0x200D  # ZWJ


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def norm(s: str) -> str:
    """Full normalization used for name comparison."""
    s = nfkc(s)
    s = "".join(ch for ch in s if not _is_stripped(ch))
    s = _WS.sub("", s)
    return s.casefold()


def cjk(s: str) -> str:
    """Concatenated CJK-ideograph runs (from the NFKC form)."""
    return "".join(_CJK_RUN.findall(nfkc(s or "")))


def ascii_part(s: str) -> str:
    """Non-CJK remainder of norm(s), trailing ``_-.`` punctuation stripped."""
    n = norm(s)
    rest = _CJK_RUN.sub("", n)
    return rest.rstrip("_-.")


def header_key(s: str) -> str:
    """Canonical header form: NFKC, all whitespace removed, casefolded.

    Handles the observed quirks — full-width paren in ``FAN BASE（K)`` and the
    double space in ``TTL  ENGAGEMENT`` — so future exports with cosmetic
    header drift still map.
    """
    s = nfkc(s or "")
    s = _WS.sub("", s)
    return s.casefold()


HEX24 = re.compile(r"\b([0-9a-fA-F]{24})\b")


def is_hex24(s: str) -> bool:
    s = (s or "").strip()
    return len(s) == 24 and bool(re.fullmatch(r"[0-9a-fA-F]{24}", s))
