"""DMR Reconciler — FastAPI web app.

Flow: upload PLOG.xlsx + DMR.xlsx → parse preview (detected header rows, row
counts, campaign sections, DMR date window; user confirms) → background run
with live progress → results table with per-row evidence and human overrides
→ annotated .xlsx / JSON audit exports, plus the reverse-audit tab.
"""
from __future__ import annotations

import hmac
import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import auth, config, db, runner
from .matcher import LINK_ERROR, MATCH, NO_BLOGGER, NO_POST, REVIEW, NAME_MISLABEL
from .parsers import parse_dmr, parse_plog
from .report import OVERRIDE_MATCH_BLANK, build_audit_json, write_annotated_xlsx

app = FastAPI(title="DMR Reconciler")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fromjson"] = json.loads

_start_lock = threading.Lock()

STATUS_BADGES = {
    MATCH: ("match", "MATCH"),
    NO_POST: ("nopost", "无帖子 NO_POST"),
    NO_BLOGGER: ("noblogger", "无博主 NO_BLOGGER"),
    LINK_ERROR: ("linkerror", "Check链接错误 LINK_ERROR"),
    REVIEW: ("review", "人工复核 REVIEW"),
}
OVERRIDE_CHOICES = ["", OVERRIDE_MATCH_BLANK, "无博主", "无帖子", "Check链接错误",
                    NAME_MISLABEL, "人工复核"]


@app.on_event("startup")
async def recover_orphaned_runs() -> None:
    """A deploy/restart (or Fly auto-stop) kills in-flight daemon threads;
    their runs would otherwise stay 'running' forever with no restart path."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE runs SET status='error', phase='error', "
            "message='Run interrupted by a restart — use Retry.' "
            "WHERE status='running' OR status='queued'"
        )
        conn.commit()


# ------------------------------------------------------------------- auth
#
# APP_PASSWORD is the *setup code*: /setup (which requires it) creates the
# first admin account; admins add coworkers on /team. Without APP_PASSWORD
# the app runs open (local development).

def current_user(request: Request) -> Optional[dict]:
    username = auth.read_session(request.cookies.get("dmr_session", ""))
    if not username:
        return None
    return db.user_get(username)


templates.env.globals["user_of"] = current_user


def _session_response(username: str, url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie("dmr_session", auth.make_session(username), httponly=True,
                    max_age=auth.SESSION_TTL, samesite="lax")
    return resp


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/healthz", "/login", "/setup") or path.startswith("/static"):
        return await call_next(request)
    if not config.APP_PASSWORD:
        return await call_next(request)
    if current_user(request):
        return await call_next(request)
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "error": "", "no_users": db.user_count() == 0})


@app.post("/login")
async def login(request: Request, username: str = Form(""),
                password: str = Form(...)):
    username = auth.normalize_username(username)
    user = db.user_get(username) if username else None
    if user and auth.verify_password(password, user["password_hash"]):
        return _session_response(username)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": "用户名或密码错误 / wrong username or password",
         "no_users": db.user_count() == 0},
        status_code=401)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("dmr_session")
    return resp


@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    return templates.TemplateResponse(request, "setup.html", {
        "error": "", "auth_enabled": bool(config.APP_PASSWORD),
        "has_users": db.user_count() > 0})


@app.post("/setup")
async def setup(request: Request, code: str = Form(...),
                username: str = Form(...), password: str = Form(...),
                display: str = Form("")):
    def fail(msg: str, status: int = 400):
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": msg, "auth_enabled": bool(config.APP_PASSWORD),
             "has_users": db.user_count() > 0},
            status_code=status)

    if not config.APP_PASSWORD:
        return fail("APP_PASSWORD is not configured — authentication is disabled.")
    if not hmac.compare_digest(code.encode(), config.APP_PASSWORD.encode()):
        return fail("设置码错误 / wrong setup code", 401)
    username = auth.normalize_username(username)
    if not auth.valid_username(username):
        return fail("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)")
    if len(password) < 8:
        return fail("Password must be at least 8 characters.")
    db.user_upsert(username, auth.hash_password(password),
                   display=display.strip(), is_admin=True)
    return _session_response(username)


# ------------------------------------------------------------------- team

@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, msg: str = "", error: str = ""):
    user = current_user(request)
    return templates.TemplateResponse(request, "team.html", {
        "user": user, "users": db.user_list(),
        "msg": msg, "error": error,
        "auth_enabled": bool(config.APP_PASSWORD),
    })


def _team_redirect(msg: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in (("msg", msg), ("error", error)) if v})
    return RedirectResponse(f"/team?{q}", status_code=303)


@app.post("/team/add")
async def team_add(request: Request, username: str = Form(...),
                   password: str = Form(...), display: str = Form(""),
                   is_admin: str = Form("0")):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error="Only admins can add accounts.")
    username = auth.normalize_username(username)
    if not auth.valid_username(username):
        return _team_redirect(error="Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)")
    if db.user_get(username):
        return _team_redirect(error=f"User {username} already exists.")
    if len(password) < 8:
        return _team_redirect(error="Password must be at least 8 characters.")
    db.user_upsert(username, auth.hash_password(password),
                   display=display.strip(), is_admin=is_admin == "1")
    return _team_redirect(msg=f"Account {username} created — share the initial "
                              "password with them privately.")


@app.post("/team/delete")
async def team_delete(request: Request, username: str = Form(...)):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error="Only admins can remove accounts.")
    username = auth.normalize_username(username)
    target = db.user_get(username)
    if not target:
        return _team_redirect(error="No such user.")
    if username == user["username"]:
        return _team_redirect(error="You cannot delete your own account.")
    if target["is_admin"] and db.admin_count() <= 1:
        return _team_redirect(error="Cannot delete the last admin.")
    db.user_delete(username)
    return _team_redirect(msg=f"Account {username} removed.")


@app.post("/team/password")
async def team_password(request: Request, username: str = Form(...),
                        password: str = Form(...)):
    user = current_user(request)
    if not user:
        return _team_redirect(error="Not signed in.")
    username = auth.normalize_username(username)
    if username != user["username"] and not user["is_admin"]:
        return _team_redirect(error="Only admins can reset other passwords.")
    if not db.user_get(username):
        return _team_redirect(error="No such user.")
    if len(password) < 8:
        return _team_redirect(error="Password must be at least 8 characters.")
    db.user_set_password(username, auth.hash_password(password))
    return _team_redirect(msg=f"Password updated for {username}.")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "runs": db.run_list(),
        "tikhub_configured": bool(config.TIKHUB_API_KEY),
        "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
        "model": config.ANTHROPIC_MODEL,
    })


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, plog: UploadFile, dmr: UploadFile):
    config.ensure_dirs()
    run_id = uuid.uuid4().hex[:12]
    run_dir = config.UPLOAD_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    plog_path = run_dir / ("plog_" + Path(plog.filename or "plog.xlsx").name)
    dmr_path = run_dir / ("dmr_" + Path(dmr.filename or "dmr.xlsx").name)
    plog_path.write_bytes(await plog.read())
    dmr_path.write_bytes(await dmr.read())

    try:
        # openpyxl parsing is CPU-bound; keep it off the event loop so
        # /healthz and progress polls stay responsive during big uploads.
        p = await run_in_threadpool(parse_plog, str(plog_path))
        d = await run_in_threadpool(parse_dmr, str(dmr_path))
    except ValueError as e:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(e)}, status_code=422)
    except Exception as e:  # corrupt zip, wrong format, … — never a 500
        return templates.TemplateResponse(
            request, "error.html",
            {"message": f"Could not read the uploaded file(s) as .xlsx: {e}"},
            status_code=422)

    d_from, d_to = p.date_range
    out_of_window = 0
    if d.window_from and d.window_to:
        out_of_window = sum(
            1 for r in p.rows
            if r.post_date and not (d.window_from <= r.post_date <= d.window_to))
    preview = {
        "plog": {
            "sheet": p.sheet, "header_row": p.header_row, "rows": len(p.rows),
            "campaigns": [
                {"name": c, "rows": sum(1 for r in p.rows if r.campaign == c)}
                for c in p.campaigns],
            "date_range": [str(d_from) if d_from else None,
                           str(d_to) if d_to else None],
            "warnings": p.warnings,
        },
        "dmr": {
            "sheet": d.sheet, "header_row": d.header_row, "rows": len(d.rows),
            "window": [str(d.window_from) if d.window_from else None,
                       str(d.window_to) if d.window_to else None],
            "warnings": d.warnings,
        },
        "out_of_window_rows": out_of_window,
    }
    db.run_create(run_id, plog_path=str(plog_path), dmr_path=str(dmr_path),
                  plog_name=plog.filename, dmr_name=dmr.filename,
                  preview=preview)
    return templates.TemplateResponse(request, "preview.html", {
        "run_id": run_id, "preview": preview,
        "tikhub_configured": bool(config.TIKHUB_API_KEY),
        "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
    })


@app.post("/runs/{run_id}/start")
async def start(run_id: str, retry_failed_links: str = Form("0"),
                use_llm: str = Form("0")):
    """Checkbox values arrive as "1" (hidden-input fallback supplies "0" when
    unchecked — a bool Form default can never receive False from a form)."""
    with _start_lock:  # two concurrent POSTs must not spawn two run threads
        run = db.run_get(run_id)
        if not run:
            return Response(status_code=404)
        if run["status"] in ("pending", "error"):
            db.run_update(run_id, options_json=json.dumps({
                "retry_failed_links": retry_failed_links == "1",
                "use_llm": use_llm == "1",
            }), status="queued", error=None)
            runner.start_run(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str):
    run = db.run_get(run_id)
    if not run:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Run not found"}, status_code=404)
    return templates.TemplateResponse(request, "run.html", {
        "run": run, "run_id": run_id,
        "preview": json.loads(run.get("preview_json") or "{}"),
    })


@app.get("/runs/{run_id}/progress", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    """htmx polling target — swaps in a progress bar, the results, or the
    error panel. The done/error branches retarget #run-body, which removes the
    polling element and therefore stops the poll loop."""
    run = db.run_get(run_id)
    if not run:
        return Response(status_code=404)
    if run["status"] == "done":
        resp = await results_fragment(request, run_id)
        resp.headers["HX-Retarget"] = "#run-body"
        return resp
    if run["status"] == "error":
        resp = templates.TemplateResponse(
            request, "_error_panel.html",
            {"run": run, "run_id": run_id,
             "options": json.loads(run.get("options_json") or "{}")})
        resp.headers["HX-Retarget"] = "#run-body"
        return resp
    return templates.TemplateResponse(request, "_progress.html", {"run": run})


async def results_fragment(request: Request, run_id: str):
    run = db.run_get(run_id)
    if not run:
        return Response("run not found", status_code=404)
    result = json.loads(run.get("result_json") or "{}")
    verdicts = result.get("verdicts", [])
    overrides = db.overrides_for_run(run_id)
    for v in verdicts:
        v["override"] = overrides.get(v["excel_row"])
    summary = json.loads(run.get("summary_json") or "{}")
    return templates.TemplateResponse(request, "_results.html", {
        "run": run, "run_id": run_id, "verdicts": verdicts,
        "counts": result.get("counts", {}),
        "reverse_rows": result.get("reverse_audit", []),
        "plog_meta": result.get("plog_meta", {}),
        "dmr_meta": result.get("dmr_meta", {}),
        "summary": summary,
        "badges": STATUS_BADGES, "override_choices": OVERRIDE_CHOICES,
    })


@app.get("/runs/{run_id}/results", response_class=HTMLResponse)
async def results(request: Request, run_id: str):
    return await results_fragment(request, run_id)


@app.post("/runs/{run_id}/override", response_class=HTMLResponse)
async def set_override(request: Request, run_id: str,
                       excel_row: int = Form(...),
                       campaign: str = Form(""), no: str = Form(""),
                       status: str = Form(""), note: str = Form("")):
    if not db.run_get(run_id):
        return Response("run not found", status_code=404)
    user = current_user(request)
    if status:
        db.override_set(run_id, excel_row, campaign, no, status, note,
                        updated_by=user["username"] if user else "")
    else:
        db.override_clear(run_id, excel_row)
    return await results_fragment(request, run_id)


# ----------------------------------------------------------------- exports

def _run_or_404(run_id: str):
    run = db.run_get(run_id)
    if not run or run["status"] != "done":
        return None
    return run


@app.get("/runs/{run_id}/export.xlsx")
async def export_xlsx(run_id: str):
    run = _run_or_404(run_id)
    if not run:
        return Response("run not finished", status_code=404)
    result = json.loads(run["result_json"])
    verdicts = runner.load_verdicts(run)
    overrides = db.overrides_for_run(run_id)
    # a stable per-run path — overwritten on re-export, no /tmp leak
    out = Path(run["plog_path"]).parent / f"PLOG_DMR_CHECK_{run_id}.xlsx"
    await run_in_threadpool(
        write_annotated_xlsx,
        run["plog_path"], str(out), verdicts,
        result.get("plog_meta", {}).get("header_row", 1),
        result.get("plog_meta", {}).get("sheet"),
        overrides,
    )
    return FileResponse(str(out), filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/runs/{run_id}/export.json")
async def export_json(run_id: str):
    run = _run_or_404(run_id)
    if not run:
        return Response("run not finished", status_code=404)
    result = json.loads(run["result_json"])
    verdicts = runner.load_verdicts(run)
    overrides = db.overrides_for_run(run_id)
    doc = build_audit_json(run, verdicts, result.get("counts", {}),
                           result.get("plog_meta", {}),
                           result.get("dmr_meta", {}),
                           result.get("reverse_audit", []),
                           overrides=overrides)
    return Response(doc, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="audit_{run_id}.json"'})


@app.get("/runs/{run_id}/api")
async def run_api(run_id: str):
    """Raw JSON progress/status for programmatic polling."""
    run = db.run_get(run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": run["id"], "status": run["status"], "phase": run["phase"],
        "progress_done": run["progress_done"],
        "progress_total": run["progress_total"],
        "message": run["message"], "tikhub_calls": run["tikhub_calls"],
        "llm_calls": run["llm_calls"],
    }
