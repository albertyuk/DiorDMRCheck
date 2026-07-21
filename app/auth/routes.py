"""Auth + team routes and the session-gate middleware.

APP_PASSWORD is the *setup code*: /setup (which requires it) creates the
first admin account; admins add coworkers on /team. Without APP_PASSWORD
the app runs open (local development).
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..core import db
from ..web import current_user, templates, tr as _tr
from . import service

router = APIRouter()


def _session_response(username: str, url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie("dmr_session", service.make_session(username),
                    httponly=True, max_age=service.SESSION_TTL, samesite="lax")
    return resp


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (path in ("/healthz", "/login", "/setup")
            or path.startswith("/static") or path.startswith("/lang/")):
        return await call_next(request)
    if not config.APP_PASSWORD:
        return await call_next(request)
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
    user = db.user_get(username) if username else None
    if user and service.verify_password(password, user["password_hash"]):
        return _session_response(username)
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
    if not hmac.compare_digest(code.encode(), config.APP_PASSWORD.encode()):
        return fail(tr("Wrong setup code."), 401)
    username = service.normalize_username(username)
    if not service.valid_username(username):
        return fail(tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if len(password) < 8:
        return fail(tr("Password must be at least 8 characters."))
    db.user_upsert(username, service.hash_password(password),
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
    from urllib.parse import urlencode
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
    if db.user_get(username):
        return _team_redirect(error=tr("User {username} already exists.",
                                       username=username))
    if len(password) < 8:
        return _team_redirect(error=tr("Password must be at least 8 characters."))
    db.user_upsert(username, service.hash_password(password),
                   display=display.strip(), is_admin=is_admin == "1")
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
    target = db.user_get(username)
    if not target:
        return _team_redirect(error=tr("No such user."))
    if username == user["username"]:
        return _team_redirect(error=tr("You cannot delete your own account."))
    if target["is_admin"] and db.admin_count() <= 1:
        return _team_redirect(error=tr("Cannot delete the last admin."))
    db.user_delete(username)
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
    db.user_set_password(username, service.hash_password(password))
    return _team_redirect(msg=tr("Password updated for {username}.",
                                 username=username))
