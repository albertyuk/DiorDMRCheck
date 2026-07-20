"""DMR Reconciler — FastAPI web app.

Flow: upload PLOG.xlsx + DMR.xlsx → parse preview (detected header rows, row
counts, campaign sections, DMR date window; user confirms) → background run
with live progress → results table with per-row evidence and human overrides
→ annotated .xlsx / JSON audit exports, plus the reverse-audit tab.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.templating import Jinja2Templates

from . import config, db, runner
from .matcher import LINK_ERROR, MATCH, NO_BLOGGER, NO_POST, REVIEW, NAME_MISLABEL
from .parsers import parse_dmr, parse_plog
from .report import build_audit_json, write_annotated_xlsx

app = FastAPI(title="DMR Reconciler")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_BADGES = {
    MATCH: ("match", "MATCH"),
    NO_POST: ("nopost", "无帖子 NO_POST"),
    NO_BLOGGER: ("noblogger", "无博主 NO_BLOGGER"),
    LINK_ERROR: ("linkerror", "Check链接错误 LINK_ERROR"),
    REVIEW: ("review", "人工复核 REVIEW"),
}
OVERRIDE_CHOICES = ["", "无博主", "无帖子", "Check链接错误", NAME_MISLABEL, "人工复核"]


# ------------------------------------------------------------------- auth

def _sign(value: str) -> str:
    return hmac.new(config.APP_SECRET.encode(), value.encode(),
                    hashlib.sha256).hexdigest()


def _session_ok(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    token = request.cookies.get("dmr_session", "")
    return hmac.compare_digest(token, _sign("authenticated"))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/healthz", "/login") or path.startswith("/static"):
        return await call_next(request)
    if not _session_ok(request):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if config.APP_PASSWORD and hmac.compare_digest(password, config.APP_PASSWORD):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("dmr_session", _sign("authenticated"), httponly=True,
                        max_age=7 * 24 * 3600, samesite="lax")
        return resp
    return templates.TemplateResponse(
        request, "login.html", {"error": "Wrong password"}, status_code=401)


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
        p = parse_plog(str(plog_path))
        d = parse_dmr(str(dmr_path))
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
async def start(run_id: str, retry_failed_links: bool = Form(False),
                use_llm: bool = Form(True)):
    run = db.run_get(run_id)
    if not run:
        return Response(status_code=404)
    if run["status"] in ("pending", "error"):
        db.run_update(run_id, options_json=json.dumps({
            "retry_failed_links": bool(retry_failed_links),
            "use_llm": bool(use_llm),
        }), status="pending", error=None)
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
    """htmx polling target — swaps in either a progress bar or the results."""
    run = db.run_get(run_id)
    if not run:
        return Response(status_code=404)
    if run["status"] == "done":
        resp = await results_fragment(request, run_id)
        resp.headers["HX-Retarget"] = "#run-body"
        return resp
    return templates.TemplateResponse(request, "_progress.html", {"run": run})


async def results_fragment(request: Request, run_id: str):
    run = db.run_get(run_id)
    result = json.loads(run.get("result_json") or "{}")
    verdicts = result.get("verdicts", [])
    overrides = db.overrides_for_run(run_id)
    for v in verdicts:
        ov = overrides.get((v["campaign"], v["no"]))
        v["override"] = ov
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
                       campaign: str = Form(""), no: str = Form(""),
                       status: str = Form(""), note: str = Form("")):
    if status:
        db.override_set(run_id, campaign, no, status, note)
    else:
        db.override_clear(run_id, campaign, no)
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
    out = Path(tempfile.mkdtemp()) / f"PLOG_DMR_CHECK_{run_id}.xlsx"
    write_annotated_xlsx(
        run["plog_path"], str(out), verdicts,
        header_row=result.get("plog_meta", {}).get("header_row", 1),
        overrides=overrides,
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
