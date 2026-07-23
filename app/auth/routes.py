"""Auth + team routes and the session-gate middleware.

APP_PASSWORD is the *setup code*: /setup (which requires it) creates the
first admin account; admins add coworkers on /team. Passwordless local mode
requires the explicit ALLOW_OPEN_ACCESS opt-out.
"""
from __future__ import annotations

import hmac
import logging
from urllib.parse import urlencode
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import config
from ..core import db
from ..web import current_user, templates, tr as _tr
from . import service, throttle

router = APIRouter()
logger = logging.getLogger(__name__)


def _browser_origin_allowed(request: Request) -> bool:
    """Reject browser-submitted unsafe requests from another origin.

    Browser form POSTs carry ``Origin`` and Fetch Metadata. Non-browser API
    clients and the existing test harness may omit both, so absence alone is
    not rejected; an explicit cross/same-site signal or mismatched origin is.
    This complements the SameSite cookie and specifically blocks a compromised
    sibling site from driving privileged team-management requests.
    """
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return True
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    host = request.headers.get("host", "").casefold()
    return (parsed.scheme in {"http", "https"}
            and parsed.netloc.casefold() == host)


def _session_response(username: str, url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie("dmr_session", service.make_session(username),
                    httponly=True, max_age=service.SESSION_TTL, samesite="lax",
                    secure=config.SESSION_COOKIE_SECURE)
    return resp


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not _browser_origin_allowed(request):
        logger.warning("blocked cross-origin state change path=%s", path)
        return PlainTextResponse("Cross-origin request rejected.",
                                 status_code=403)
    if (path in ("/healthz", "/login", "/setup")
            or path.startswith("/static") or path.startswith("/lang/")):
        return await call_next(request)
    if not config.APP_PASSWORD:
        if config.ALLOW_OPEN_ACCESS:
            return await call_next(request)
        # Lifespan validation normally prevents this state. Keep middleware
        # fail-closed for ASGI harnesses that bypass lifespan startup.
        return PlainTextResponse("Authentication is not configured.",
                                 status_code=503)
    if current_user(request):
        return await call_next(request)
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {
        "error": "", "no_users": db.user_count() == 0})


@router.post("/login")
async def login(request: Request, username: str = Form(""),
                password: str = Form(...)):
    username = service.normalize_username(username)
    ip = throttle.client_ip(request)
    reservation, wait = throttle.reserve(("user", username), ("ip", ip))
    if reservation is None:
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": _tr(request)(
                "Too many failed attempts — wait {s} seconds and try again.",
                s=wait),
             "no_users": db.user_count() == 0},
            status_code=429)
    user = db.user_get(username) if username else None
    # PBKDF2 (~16 ms) off the event loop, so a guess burst cannot stall
    # every other request in this single-process app
    stored_hash = (user or {}).get("password_hash", service.DUMMY_PASSWORD_HASH)
    try:
        verified = await run_in_threadpool(
            service.verify_password, password, stored_hash)
        ok = bool(user) and verified
    except BaseException:
        throttle.complete(reservation, failed=True)
        raise
    if ok:
        throttle.complete(reservation, failed=False, clear_scopes=("user",))
        return _session_response(username)
    throttle.complete(reservation, failed=True)
    return templates.TemplateResponse(
        request, "auth/login.html",
        {"error": _tr(request)("Wrong username or password."),
         "no_users": db.user_count() == 0},
        status_code=401)


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("dmr_session")
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    return templates.TemplateResponse(request, "auth/setup.html", {
        "error": "", "auth_enabled": bool(config.APP_PASSWORD),
        "has_users": db.user_count() > 0})


@router.post("/setup")
async def setup(request: Request, code: str = Form(...),
                username: str = Form(...), password: str = Form(...),
                display: str = Form("")):
    def fail(msg: str, status: int = 400):
        return templates.TemplateResponse(
            request, "auth/setup.html",
            {"error": msg, "auth_enabled": bool(config.APP_PASSWORD),
             "has_users": db.user_count() > 0},
            status_code=status)

    tr = _tr(request)
    if not config.APP_PASSWORD:
        return fail(tr("APP_PASSWORD is not configured — authentication is disabled."))
    ip = throttle.client_ip(request)
    reservation, wait = throttle.reserve(("setup", ip))
    if reservation is None:
        return fail(tr(
            "Too many failed attempts — wait {s} seconds and try again.",
            s=wait), 429)
    if not hmac.compare_digest(code.encode(), config.APP_PASSWORD.encode()):
        throttle.complete(reservation, failed=True)
        return fail(tr("Wrong setup code."), 401)
    throttle.complete(reservation, failed=False, clear_scopes=("setup",))
    username = service.normalize_username(username)
    if not service.valid_username(username):
        return fail(tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if len(password) < 8:
        return fail(tr("Password must be at least 8 characters."))
    db.user_upsert(username, await run_in_threadpool(service.hash_password,
                                                     password),
                   display=display.strip(), is_admin=True)
    return _session_response(username)


# ------------------------------------------------------------------- team

@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, msg: str = "", error: str = ""):
    user = current_user(request)
    return templates.TemplateResponse(request, "auth/team.html", {
        "user": user, "users": db.user_list(),
        "msg": msg, "error": error,
        "auth_enabled": bool(config.APP_PASSWORD),
    })


def _team_redirect(msg: str = "", error: str = "") -> RedirectResponse:
    q = urlencode({k: v for k, v in (("msg", msg), ("error", error)) if v})
    return RedirectResponse(f"/team?{q}", status_code=303)


@router.post("/team/add")
async def team_add(request: Request, username: str = Form(...),
                   password: str = Form(...), display: str = Form(""),
                   is_admin: str = Form("0")):
    user = current_user(request)
    tr = _tr(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can add accounts."))
    username = service.normalize_username(username)
    if not service.valid_username(username):
        return _team_redirect(error=tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if len(password) < 8:
        return _team_redirect(error=tr("Password must be at least 8 characters."))
    password_hash = await run_in_threadpool(service.hash_password, password)
    if not db.user_create(username, password_hash, display=display.strip(),
                          is_admin=is_admin == "1"):
        return _team_redirect(error=tr("User {username} already exists.",
                                       username=username))
    return _team_redirect(msg=tr("Account {username} created — share the "
                                 "initial password with them privately.",
                                 username=username))


@router.post("/team/delete")
async def team_delete(request: Request, username: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can remove accounts."))
    username = service.normalize_username(username)
    outcome = db.user_delete_guarded(user["username"], username)
    if outcome == "forbidden":
        return _team_redirect(error=tr("Only admins can remove accounts."))
    if outcome == "missing":
        return _team_redirect(error=tr("No such user."))
    if outcome == "self":
        return _team_redirect(error=tr("You cannot delete your own account."))
    if outcome == "last_admin":
        return _team_redirect(error=tr("Cannot delete the last admin."))
    return _team_redirect(msg=tr("Account {username} removed.",
                                 username=username))


@router.post("/team/password")
async def team_password(request: Request, username: str = Form(...),
                        password: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user:
        return _team_redirect(error=tr("Not signed in."))
    username = service.normalize_username(username)
    if username != user["username"] and not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can reset other passwords."))
    if not db.user_get(username):
        return _team_redirect(error=tr("No such user."))
    if len(password) < 8:
        return _team_redirect(error=tr("Password must be at least 8 characters."))
    password_hash = await run_in_threadpool(service.hash_password, password)
    db.user_set_password(username, password_hash)
    return _team_redirect(msg=tr("Password updated for {username}.",
                                 username=username))
