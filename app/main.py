"""DMR Reconciler — FastAPI web app.

Flow: upload PLOG.xlsx + DMR.xlsx → parse preview (detected header rows, row
counts, campaign sections, DMR date window; user confirms) → background run
with live progress → results table with per-row evidence and human overrides
→ annotated .xlsx / JSON audit exports, plus the reverse-audit tab.
"""
from __future__ import annotations

import hmac
import io
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import auth, config, db, i18n, perimeter as perimeter_mod, runner
from .deck import DONUT_COLORS, assert_chart_cache, build_deck
from .effreport import (COOPS, TIERS, ReportConfig, VerificationError,
                        analyze as analyze_efficiency)
from .matcher import (LINK_ERROR, MATCH, NO_BLOGGER,
                      NO_BLOGGER_NOT_IN_PERIMETER, NO_POST,
                      NO_POST_IN_PERIMETER, REVIEW, NAME_MISLABEL, S_TEXT)
from .parsers import parse_dmr, parse_plog
from .report import OVERRIDE_MATCH_BLANK, build_audit_json, write_annotated_xlsx

app = FastAPI(title="DMR Reconciler")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"),
                            context_processors=[i18n.context])
templates.env.filters["fromjson"] = json.loads


def _tr(request: Request):
    """Translator for messages built inside handlers (same t as templates)."""
    return i18n.make_t(i18n.get_lang(request))


def _td(request: Request):
    """Pattern translator for dynamic English text (parser errors etc.)."""
    return i18n.make_td(i18n.get_lang(request))

_start_lock = threading.Lock()

STATUS_BADGES = {
    MATCH: ("match", "MATCH"),
    NO_POST: ("nopost", "无帖子 NO_POST"),
    NO_BLOGGER: ("noblogger", "无博主 NO_BLOGGER"),
    LINK_ERROR: ("linkerror", "Check链接错误 LINK_ERROR"),
    REVIEW: ("review", "人工复核 REVIEW"),
    NO_POST_IN_PERIMETER: ("periin", "Perimeter内 无帖子"),
    NO_BLOGGER_NOT_IN_PERIMETER: ("periout", "不在Perimeter"),
}
OVERRIDE_CHOICES = ["", OVERRIDE_MATCH_BLANK, "无博主", "无帖子", "Check链接错误",
                    NAME_MISLABEL, "人工复核",
                    S_TEXT[NO_POST_IN_PERIMETER],
                    S_TEXT[NO_BLOGGER_NOT_IN_PERIMETER]]


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


@app.get("/lang/{code}")
async def set_lang(request: Request, code: str):
    """Top-left toggle target — remembers the choice for a year and returns
    to the page the user was on (path only, so the redirect can't leave the
    site)."""
    if code not in i18n.SUPPORTED:
        code = "en"
    from urllib.parse import urlparse
    ref = urlparse(request.headers.get("referer", ""))
    back = ref.path or "/"
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    elif ref.query:  # keep e.g. /team?msg=… flash messages across the toggle
        back += "?" + ref.query
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie(i18n.COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
    return resp


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
        {"error": _tr(request)("Wrong username or password."),
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

    tr = _tr(request)
    if not config.APP_PASSWORD:
        return fail(tr("APP_PASSWORD is not configured — authentication is disabled."))
    if not hmac.compare_digest(code.encode(), config.APP_PASSWORD.encode()):
        return fail(tr("Wrong setup code."), 401)
    username = auth.normalize_username(username)
    if not auth.valid_username(username):
        return fail(tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if len(password) < 8:
        return fail(tr("Password must be at least 8 characters."))
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
    tr = _tr(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can add accounts."))
    username = auth.normalize_username(username)
    if not auth.valid_username(username):
        return _team_redirect(error=tr("Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)"))
    if db.user_get(username):
        return _team_redirect(error=tr("User {username} already exists.",
                                       username=username))
    if len(password) < 8:
        return _team_redirect(error=tr("Password must be at least 8 characters."))
    db.user_upsert(username, auth.hash_password(password),
                   display=display.strip(), is_admin=is_admin == "1")
    return _team_redirect(msg=tr("Account {username} created — share the "
                                 "initial password with them privately.",
                                 username=username))


@app.post("/team/delete")
async def team_delete(request: Request, username: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user or not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can remove accounts."))
    username = auth.normalize_username(username)
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


@app.post("/team/password")
async def team_password(request: Request, username: str = Form(...),
                        password: str = Form(...)):
    user = current_user(request)
    tr = _tr(request)
    if not user:
        return _team_redirect(error=tr("Not signed in."))
    username = auth.normalize_username(username)
    if username != user["username"] and not user["is_admin"]:
        return _team_redirect(error=tr("Only admins can reset other passwords."))
    if not db.user_get(username):
        return _team_redirect(error=tr("No such user."))
    if len(password) < 8:
        return _team_redirect(error=tr("Password must be at least 8 characters."))
    db.user_set_password(username, auth.hash_password(password))
    return _team_redirect(msg=tr("Password updated for {username}.",
                                 username=username))


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
        "perimeter": perimeter_mod.current_meta(),
    })


@app.post("/perimeter/remove")
async def perimeter_remove():
    db.setting_delete("current_perimeter")
    return RedirectResponse("/", status_code=303)


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, plog: UploadFile, dmr: UploadFile,
                 perimeter: Optional[UploadFile] = File(None)):
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
            request, "error.html", {"message": _td(request)(str(e))},
            status_code=422)
    except Exception as e:  # corrupt zip, wrong format, … — never a 500
        return templates.TemplateResponse(
            request, "error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file(s) as .xlsx: {e}", e=e)},
            status_code=422)

    # Optional perimeter: a new upload replaces the persisted one; otherwise
    # the last uploaded perimeter (if any) is reused for this run.
    perim_meta = None
    perim_warnings: list[str] = []
    if perimeter is not None and perimeter.filename:
        data = await perimeter.read()
        try:
            parsed = await run_in_threadpool(
                perimeter_mod.ingest, data, Path(perimeter.filename).name)
            perim_warnings = parsed.warnings
        except ValueError as e:
            return templates.TemplateResponse(
                request, "error.html", {"message": _td(request)(str(e))},
                status_code=422)
        except Exception as e:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": _tr(request)(
                    "Could not read the perimeter file: {e}", e=e)},
                status_code=422)
    perim_meta = perimeter_mod.current_meta()

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
        "perimeter": ({**perim_meta, "warnings": perim_warnings}
                      if perim_meta else None),
    }
    db.run_create(run_id, plog_path=str(plog_path), dmr_path=str(dmr_path),
                  plog_name=plog.filename, dmr_name=dmr.filename,
                  preview=preview,
                  perimeter_hash=perim_meta["hash"] if perim_meta else None)
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
            request, "error.html", {"message": _tr(request)("Run not found")},
            status_code=404)
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
        "buckets": result.get("buckets"),
        "perimeter_meta": result.get("perimeter_meta"),
        "perimeter_warning": result.get("perimeter_warning"),
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


# ------------------------------------------------- KOL efficiency report
#
# Client campaign data: the uploaded workbook is analyzed straight from
# memory and never written to disk or the database. Finished reports live in
# a small in-process store (TTL + cap) just long enough to be downloaded.

_EFF_LOCK = threading.Lock()
_EFF_REPORTS: dict[str, dict] = {}
_EFF_TTL_SECONDS = 2 * 3600
_EFF_MAX = 20

EFF_BASES = {"pooled", "per_post"}
EFF_TIER_MODES = {"label", "fanbase"}
EFF_LANGUAGES = {"en", "zh"}


def _eff_store(entry: dict) -> str:
    token = uuid.uuid4().hex[:16]
    now = time.time()
    with _EFF_LOCK:
        for k in [k for k, v in _EFF_REPORTS.items()
                  if now - v["created"] > _EFF_TTL_SECONDS]:
            del _EFF_REPORTS[k]
        while len(_EFF_REPORTS) >= _EFF_MAX:
            oldest = min(_EFF_REPORTS, key=lambda k: _EFF_REPORTS[k]["created"])
            del _EFF_REPORTS[oldest]
        _EFF_REPORTS[token] = {**entry, "created": now}
    return token


def _eff_get(token: str) -> Optional[dict]:
    with _EFF_LOCK:
        entry = _EFF_REPORTS.get(token)
        if entry and time.time() - entry["created"] > _EFF_TTL_SECONDS:
            del _EFF_REPORTS[token]
            return None
        return entry


def _eff_report_context(token: str, entry: dict) -> dict:
    analysis = entry["analysis"]
    groups = analysis["metrics"]["groups"]
    ordered = [(f"{t} {c}", groups[f"{t} {c}"])
               for t in TIERS for c in COOPS if f"{t} {c}" in groups]
    return {
        "token": token,
        "filename": entry["filename"],
        "analysis": analysis,
        "ordered_groups": ordered,
        "donut_colors": DONUT_COLORS,
        "has_deck": entry.get("pptx") is not None,
    }


@app.get("/efficiency", response_class=HTMLResponse)
async def efficiency_form(request: Request):
    return templates.TemplateResponse(request, "efficiency.html", {})


@app.post("/efficiency", response_class=HTMLResponse)
async def efficiency_run(request: Request, report: UploadFile,
                         basis: str = Form("pooled"),
                         tier_mode: str = Form("label"),
                         language: str = Form("en")):
    cfg = ReportConfig(
        basis=basis if basis in EFF_BASES else "pooled",
        tier_mode=tier_mode if tier_mode in EFF_TIER_MODES else "label",
        language=language if language in EFF_LANGUAGES else "en",
    )
    data = await report.read()

    def _analyze_and_build():
        analysis = analyze_efficiency(io.BytesIO(data), cfg)
        pptx = None
        if not analysis["blocked"]:
            pptx = build_deck(analysis)
            assert_chart_cache(pptx, analysis)  # never ship unverified XML
        return analysis, pptx

    try:
        analysis, pptx = await run_in_threadpool(_analyze_and_build)
    except ValueError as e:  # V1 — wrong sheet shape
        return templates.TemplateResponse(
            request, "error.html", {"message": _td(request)(str(e))},
            status_code=422)
    except VerificationError as e:
        return templates.TemplateResponse(
            request, "error.html",
            {"message": _tr(request)(
                "Internal cross-check failed — report not generated: {e}", e=e)},
            status_code=500)
    except Exception as e:  # corrupt zip, wrong format, … — never a 500
        return templates.TemplateResponse(
            request, "error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file as .xlsx: {e}", e=e)},
            status_code=422)

    filename = Path(report.filename or "report.xlsx").stem
    token = _eff_store({"analysis": analysis, "pptx": pptx,
                        "filename": filename})
    return RedirectResponse(f"/efficiency/{token}", status_code=303)


@app.get("/efficiency/{token}", response_class=HTMLResponse)
async def efficiency_report(request: Request, token: str):
    entry = _eff_get(token)
    if not entry:
        return templates.TemplateResponse(
            request, "error.html",
            {"message": _tr(request)(
                "This report has expired (reports are kept in memory for 2 "
                "hours, never stored). Re-upload the workbook.")},
            status_code=404)
    return templates.TemplateResponse(request, "efficiency_report.html",
                                      _eff_report_context(token, entry))


@app.get("/efficiency/{token}/deck.pptx")
async def efficiency_deck(token: str):
    entry = _eff_get(token)
    if not entry or entry.get("pptx") is None:
        return Response("report expired or deck blocked by validation",
                        status_code=404)
    fname = f"{entry['filename']}_efficiency.pptx"
    return Response(
        entry["pptx"],
        media_type="application/vnd.openxmlformats-officedocument"
                   ".presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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
