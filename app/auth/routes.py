"""Auth + team routes and the session-gate middleware.

APP_PASSWORD is the *setup code*: /setup (which requires it) creates the
first admin account; admins add coworkers on /team. Passwordless local mode
requires the explicit ALLOW_OPEN_ACCESS opt-out.
"""
from __future__ import annotations

import hmac
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import config
from ..core import db
from ..web import current_user, templates, tr as _tr
from . import mailer, service, throttle

router = APIRouter()

# Paths reachable WITHOUT a session (token links arrive by email, before the
# user has an account/session). Prefix-matched in the middleware.
_PUBLIC_PREFIXES = ("/static", "/lang/", "/invite/", "/reset/", "/verify/")
_PUBLIC_EXACT = ("/healthz", "/login", "/setup", "/forgot")


def _link(request: Request, path: str) -> str:
    """Absolute URL for an emailed link. Uses the configured PUBLIC_BASE_URL
    (never the request Host header, which is attacker-controllable) and falls
    back to the request origin only for local dev."""
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}{path}"


async def _send_email_verification(request: Request, username: str,
                                   email: str) -> bool:
    """Email a confirm-ownership link. No-op (False) when email is off."""
    if not config.email_enabled():
        return False
    raw = service.issue_token(username, "verify", email,
                              config.INVITE_TTL_HOURS)
    return await run_in_threadpool(
        mailer.send_verify, email, _link(request, f"/verify/{raw}"),
        config.INVITE_TTL_HOURS)


def _session_response(username: str, url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie("dmr_session", service.make_session(username),
                    httponly=True, max_age=service.SESSION_TTL, samesite="lax",
                    secure=config.SESSION_COOKIE_SECURE)
    return resp


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
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
    # Reserve a failure slot in both buckets up front and atomically: the
    # PBKDF2 verify below yields the event loop, so a plain check-then-register
    # would let concurrent guesses all pass a stale count. A correct login
    # releases its reservation.
    wait = throttle.reserve([("user", username), ("ip", ip)])
    if wait:
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
    ok = bool(user) and await run_in_threadpool(
        service.verify_password, password, user["password_hash"])
    if ok:
        throttle.clear("user", username)   # wipe this user's failures
        throttle.release("ip", ip)         # this success wasn't an IP failure
        return _session_response(username)
    # failure already counted by reserve() — do not double-register
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
        "has_users": db.user_count() > 0,
        "email_enabled": config.email_enabled()})


@router.post("/setup")
async def setup(request: Request, code: str = Form(...),
                username: str = Form(...), password: str = Form(...),
                display: str = Form(""), email: str = Form("")):
    def fail(msg: str, status: int = 400):
        return templates.TemplateResponse(
            request, "auth/setup.html",
            {"error": msg, "auth_enabled": bool(config.APP_PASSWORD),
             "has_users": db.user_count() > 0,
             "email_enabled": config.email_enabled()},
            status_code=status)

    tr = _tr(request)
    if not config.APP_PASSWORD:
        return fail(tr("APP_PASSWORD is not configured — authentication is disabled."))
    ip = throttle.client_ip(request)
    wait = throttle.retry_after("setup", ip)
    if wait:  # the setup code is the root secret — guessing it must be slow
        return fail(tr(
            "Too many failed attempts — wait {s} seconds and try again.",
            s=wait), 429)
    if not hmac.compare_digest(code.encode(), config.APP_PASSWORD.encode()):
        throttle.register_failure("setup", ip)
        return fail(tr("Wrong setup code."), 401)
    throttle.clear("setup", ip)
    username = service.normalize_username(username)
    if not service.valid_username(username):
        return fail(tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if len(password) < 8:
        return fail(tr("Password must be at least 8 characters."))
    email = service.normalize_email(email)
    if email and not service.valid_email(email):
        return fail(tr("That doesn't look like a valid email address."))
    if email:
        other = db.user_get_by_email(email)
        if other and other["username"] != username:
            return fail(tr("That email is already registered to another account."))
    # /setup doubles as "reset the admin account", so a blank email field must
    # not silently drop an address (and its confirmed status) already on file.
    existing = db.user_get(username) or {}
    verified = False
    if not email:
        email = existing.get("email") or ""
        verified = bool(email) and bool(existing.get("email_verified"))
    else:
        verified = (email == (existing.get("email") or "")
                    and bool(existing.get("email_verified")))
    db.user_upsert(username, await run_in_threadpool(service.hash_password,
                                                     password),
                   display=display.strip(), is_admin=True,
                   email=email or None, email_verified=verified)
    if email and not verified:
        # confirm ownership so the address can later receive reset links
        await _send_email_verification(request, username, email)
    return _session_response(username)


# ------------------------------------------------------------------- team

@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, msg: str = "", error: str = ""):
    user = current_user(request)
    return templates.TemplateResponse(request, "auth/team.html", {
        "user": user, "users": db.user_list(),
        "msg": msg, "error": error,
        "auth_enabled": bool(config.APP_PASSWORD),
        "email_enabled": config.email_enabled(),
    })


def _team_redirect(msg: str = "", error: str = "") -> RedirectResponse:
    q = urlencode({k: v for k, v in (("msg", msg), ("error", error)) if v})
    return RedirectResponse(f"/team?{q}", status_code=303)


@router.post("/team/add")
async def team_add(request: Request, username: str = Form(...),
                   email: str = Form(""), password: str = Form(""),
                   display: str = Form(""), is_admin: str = Form("0")):
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
    email = service.normalize_email(email)
    if email and not service.valid_email(email):
        return _team_redirect(error=tr("That doesn't look like a valid email address."))
    if email and db.user_get_by_email(email):
        return _team_redirect(error=tr("That email is already registered to another account."))

    # Invite flow: create the account WITHOUT a usable password (empty hash
    # can never authenticate) and email a set-password link.
    if email and config.email_enabled():
        db.user_upsert(username, "", display=display.strip(),
                       is_admin=is_admin == "1", email=email,
                       email_verified=False)
        raw = service.issue_token(username, "invite", email,
                                  config.INVITE_TTL_HOURS)
        sent = await run_in_threadpool(
            mailer.send_invite, email, _link(request, f"/invite/{raw}"),
            config.INVITE_TTL_HOURS, user["username"])
        if not sent:
            return _team_redirect(error=tr(
                "Account {username} created, but the invite email could not "
                "be sent. Use 'Resend invite' or set a password below.",
                username=username))
        return _team_redirect(msg=tr(
            "Invite sent to {email} — {username} sets their own password "
            "from the link.", email=email, username=username))

    # Fallback: set an initial password by hand (no email, or email disabled).
    if len(password) < 8:
        return _team_redirect(error=tr(
            "Enter an email to send an invite, or an initial password of at "
            "least 8 characters."))
    db.user_upsert(username, service.hash_password(password),
                   display=display.strip(), is_admin=is_admin == "1",
                   email=email or None, email_verified=False)
    return _team_redirect(msg=tr("Account {username} created — share the "
                                 "initial password with them privately.",
                                 username=username))


@router.post("/team/resend-invite")
async def team_resend_invite(request: Request, username: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can add accounts."))
    target = db.user_get(service.normalize_username(username))
    if not target or not target.get("email"):
        return _team_redirect(error=tr("No such user."))
    raw = service.issue_token(target["username"], "invite", target["email"],
                              config.INVITE_TTL_HOURS)
    sent = await run_in_threadpool(
        mailer.send_invite, target["email"], _link(request, f"/invite/{raw}"),
        config.INVITE_TTL_HOURS, user["username"])
    if not sent:
        return _team_redirect(error=tr("Could not send the email — check the "
                                       "email configuration."))
    return _team_redirect(msg=tr("Invite re-sent to {email}.",
                                 email=target["email"]))


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


# --------------------------------------------- invite / reset by email token

@router.post("/team/email")
async def team_email(request: Request, username: str = Form(...),
                     email: str = Form("")):
    """Set/change an account's email. Anyone may set their own; admins may
    set anyone's. A changed address is always unverified until confirmed."""
    user = current_user(request)
    tr = _tr(request)
    if not user:
        return _team_redirect(error=tr("Not signed in."))
    username = service.normalize_username(username)
    if username != user["username"] and not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can change another account's email."))
    target = db.user_get(username)
    if not target:
        return _team_redirect(error=tr("No such user."))
    email = service.normalize_email(email)
    if not email:
        db.user_set_email(username, None)
        return _team_redirect(msg=tr("Email removed from {username}.",
                                     username=username))
    if not service.valid_email(email):
        return _team_redirect(error=tr("That doesn't look like a valid email address."))
    other = db.user_get_by_email(email)
    if other and other["username"] != username:
        return _team_redirect(error=tr("That email is already registered to another account."))
    db.user_set_email(username, email)
    if await _send_email_verification(request, username, email):
        return _team_redirect(msg=tr(
            "Confirmation link sent to {email} — click it to enable password "
            "reset for this address.", email=email))
    return _team_redirect(msg=tr(
        "Email saved for {username}. It cannot be used for password reset "
        "until it is confirmed.", username=username))


@router.post("/team/verify-email")
async def team_verify_email(request: Request, username: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user:
        return _team_redirect(error=tr("Not signed in."))
    username = service.normalize_username(username)
    if username != user["username"] and not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can change another account's email."))
    target = db.user_get(username)
    if not target or not target.get("email"):
        return _team_redirect(error=tr("No such user."))
    if await _send_email_verification(request, username, target["email"]):
        return _team_redirect(msg=tr(
            "Confirmation link sent to {email} — click it to enable password "
            "reset for this address.", email=target["email"]))
    return _team_redirect(error=tr("Could not send the email — check the "
                                   "email configuration."))


_EXPIRED = ("This link has expired or was already used. Ask an admin to "
            "re-send the invite, or request a new password-reset link.")


@router.get("/verify/{token}", response_class=HTMLResponse)
async def verify_email(request: Request, token: str):
    tr = _tr(request)
    claimed = service.consume_token(token, "verify")
    ok = bool(claimed) and db.user_mark_email_verified(claimed["username"],
                                                       claimed["email"] or "")
    if not ok:
        # Either the link is spent/expired, or the account's address changed
        # after it was sent — in both cases nothing gets confirmed.
        return templates.TemplateResponse(
            request, "auth/notice.html",
            {"title": tr("Link no longer valid"), "message": tr(_EXPIRED),
             "ok": False}, status_code=404)
    return templates.TemplateResponse(request, "auth/notice.html", {
        "title": tr("Email confirmed"),
        "message": tr("This address can now receive password-reset links."),
        "ok": True})


def _set_password_page(request, action, kind, token, error="", status=200):
    return templates.TemplateResponse(
        request, "auth/set_password.html",
        {"action": action, "kind": kind, "token": token, "error": error},
        status_code=status)


def _peek_token(token: str, purpose: str) -> bool:
    """Non-destructive validity check (the token is only burned on POST)."""
    return db.auth_token_valid(service._token_hash(token), purpose) if token else False


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str):
    if not _peek_token(token, "invite"):
        return _set_password_page(request, "", "invite", token,
                                  error=_tr(request)(_EXPIRED), status=404)
    return _set_password_page(request, f"/invite/{token}", "invite", token)


@router.post("/invite/{token}")
async def invite_accept(request: Request, token: str,
                        password: str = Form(...)):
    tr = _tr(request)
    if len(password) < 8:
        return _set_password_page(request, f"/invite/{token}", "invite", token,
                                  error=tr("Password must be at least 8 characters."),
                                  status=400)
    claimed = service.consume_token(token, "invite")
    if not claimed:
        return _set_password_page(request, "", "invite", token,
                                  error=tr(_EXPIRED), status=404)
    db.user_set_password(claimed["username"],
                         await run_in_threadpool(service.hash_password, password),
                         mark_verified=True)
    return _session_response(claimed["username"])


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request):
    return templates.TemplateResponse(request, "auth/forgot.html",
                                      {"sent": False, "enabled": config.email_enabled()})


@router.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(request: Request, email: str = Form(...)):
    tr = _tr(request)
    ip = throttle.client_ip(request)
    # throttle by IP to stop reset-spam / enumeration probing
    if throttle.retry_after("setup", ip):
        return templates.TemplateResponse(
            request, "auth/forgot.html",
            {"sent": True, "enabled": config.email_enabled()})
    throttle.register_failure("setup", ip)
    email = service.normalize_email(email)
    if config.email_enabled() and service.valid_email(email):
        user = db.user_get_by_email(email)
        # Only CONFIRMED addresses receive reset links: an unconfirmed one
        # could be a typo pointing at someone else's inbox, and a reset link
        # sent there would hand them the account.
        if user and user.get("email_verified"):
            raw = service.issue_token(user["username"], "reset", email,
                                      config.RESET_TTL_HOURS)
            await run_in_threadpool(
                mailer.send_reset, email, _link(request, f"/reset/{raw}"),
                config.RESET_TTL_HOURS)
    # ALWAYS the same response — never reveal whether the email exists
    return templates.TemplateResponse(
        request, "auth/forgot.html",
        {"sent": True, "enabled": config.email_enabled()})


@router.get("/reset/{token}", response_class=HTMLResponse)
async def reset_form(request: Request, token: str):
    if not _peek_token(token, "reset"):
        return _set_password_page(request, "", "reset", token,
                                  error=_tr(request)(_EXPIRED), status=404)
    return _set_password_page(request, f"/reset/{token}", "reset", token)


@router.post("/reset/{token}")
async def reset_submit(request: Request, token: str, password: str = Form(...)):
    tr = _tr(request)
    if len(password) < 8:
        return _set_password_page(request, f"/reset/{token}", "reset", token,
                                  error=tr("Password must be at least 8 characters."),
                                  status=400)
    claimed = service.consume_token(token, "reset")
    if not claimed:
        return _set_password_page(request, "", "reset", token,
                                  error=tr(_EXPIRED), status=404)
    # new password rotates the session credential → all old sessions die
    db.user_set_password(claimed["username"],
                         await run_in_threadpool(service.hash_password, password),
                         mark_verified=True)
    db.auth_tokens_invalidate(claimed["username"], "reset")
    return _session_response(claimed["username"])
