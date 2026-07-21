"""UI localization — English default, context-aware Chinese via a header toggle.

Two translators, both keyed by the ENGLISH SOURCE TEXT so templates stay
readable and the English rendering is byte-identical to a non-localized app:

- ``t(text, **kw)``    — static template/UI strings. Looks *text* up in ``ZH``
  (exact match), falls back to the English text itself. Keyword args are
  ``str.format`` placeholders, so Chinese can reorder them freely instead of
  gluing fragments in English word order.
- ``td(text)``         — dynamic text that was *stored* at run time in English
  (progress messages, parser warnings, phase names). Tries ``ZH`` exactly,
  then the ``ZH_PATTERNS`` regexes (which carry row numbers, counts and other
  captured values into the Chinese sentence), else returns the text as-is —
  an untranslated diagnostic degrades to English, never to a broken string.

The Chinese copy is written for the actual audience — a China-side social /
KOL operations team working with 小红书, DMR exports and Perimeter lists —
not as literal translation: domain vocabulary the team already uses stays
untouched (无博主 / 无帖子 / 人工复核 / 报备 / 软植 / Perimeter / PLOG / DMR
/ CPM / CPE), a reconciliation *run* is 核对, and sentences are rebuilt
around the meaning rather than the English syntax.
"""
from __future__ import annotations

import re  # noqa: F401
from typing import Callable

from fastapi import Request

from .catalog import common, efficiency, reconciler

SUPPORTED = ("en", "zh")
COOKIE = "dmr_lang"

# Product catalogs merged at import; each product owns its strings.
ZH: dict[str, str] = {**common.ZH, **reconciler.ZH, **efficiency.ZH}
ZH_PATTERNS: list[tuple[re.Pattern[str], str]] = (
    common.ZH_PATTERNS + reconciler.ZH_PATTERNS + efficiency.ZH_PATTERNS)


def get_lang(request: Request) -> str:
    lang = request.cookies.get(COOKIE, "en")
    return lang if lang in SUPPORTED else "en"


def make_t(lang: str) -> Callable[..., str]:
    def t(text: str, **kw) -> str:
        s = ZH.get(text, text) if lang == "zh" else text
        return s.format(**kw) if kw else s
    return t


def make_td(lang: str) -> Callable[[str], str]:
    def td(text: str) -> str:
        if lang != "zh" or not text:
            return text
        hit = ZH.get(text)
        if hit is not None:
            return hit
        for pat, repl in ZH_PATTERNS:
            if pat.match(text):
                return pat.sub(repl, text)
        return text
    return td


def context(request: Request) -> dict:
    """Jinja context processor — every template gets lang / t / td."""
    lang = get_lang(request)
    return {"lang": lang, "t": make_t(lang), "td": make_td(lang)}
