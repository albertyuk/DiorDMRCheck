"""Shared web plumbing: the Jinja environment and request helpers every
router uses. Lives outside main.py so routers can import it without a
circular dependency on the app assembly."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import i18n
from .auth import service as auth_service
from .core import db

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"),
                            context_processors=[i18n.context])
templates.env.filters["fromjson"] = json.loads


def _ts(epoch) -> str:
    """Compact UTC timestamp for run lists."""
    import time as _time
    try:
        return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(float(epoch)))
    except (TypeError, ValueError):
        return ""


templates.env.filters["ts"] = _ts


def current_user(request: Request) -> Optional[dict]:
    username = auth_service.read_session(request.cookies.get("dmr_session", ""))
    if not username:
        return None
    return db.user_get(username)


templates.env.globals["user_of"] = current_user


def tr(request: Request):
    """Translator for messages built inside handlers (same t as templates)."""
    return i18n.make_t(i18n.get_lang(request))


def td(request: Request):
    """Pattern translator for dynamic English text (parser errors etc.)."""
    return i18n.make_td(i18n.get_lang(request))
