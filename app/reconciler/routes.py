"""Reconciler web flow: dashboard → upload → preview → run → results →
overrides → exports. Business logic lives in the sibling modules; handlers
orchestrate and render."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from starlette.concurrency import run_in_threadpool

from .. import config
from ..core import db
from ..remap import mapper
from ..remap.routes import FLOW_HANDLERS
from ..remap.service import PENDING_MAPS, attempt_remap, remap_note
from ..web import current_user, templates, td as _td, tr as _tr
from . import perimeter as perimeter_mod, runs
from .domain import ENGAGEMENT_CAVEAT
from .export import build_audit_json, load_verdicts, write_annotated_xlsx
from .parsers import parse_dmr, parse_plog
from .presentation import OVERRIDE_CHOICES, STATUS_BADGES

router = APIRouter()

_start_lock = threading.Lock()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "reconciler/index.html", {
        "runs": db.run_list(),
        "tikhub_configured": bool(config.TIKHUB_API_KEY),
        "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
        "model": config.ANTHROPIC_MODEL,
        "perimeter": perimeter_mod.current_meta(),
    })


@router.post("/perimeter/remove")
async def perimeter_remove():
    db.setting_delete("current_perimeter")
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ upload

def _parser_of(kind: str):
    return parse_plog if kind == "plog" else parse_dmr


async def _finish_upload(request: Request, run_id: str, run_dir: Path,
                         plog_path: Path, dmr_path: Path,
                         plog_name: str, dmr_name: str,
                         perim_data: Optional[bytes], perim_name: str,
                         remap: dict):
    """Everything after both workbooks parse: perimeter ingest, preview,
    run row, preview page. `remap` records any header mappings applied."""
    try:
        p = await run_in_threadpool(parse_plog, str(plog_path))
        d = await run_in_threadpool(parse_dmr, str(dmr_path))
    except ValueError as e:
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _td(request)(str(e))},
            status_code=422)

    perim_warnings: list[str] = []
    if perim_data:
        # Parse + cache only. The upload becomes the app-wide current
        # perimeter when this run is actually STARTED — an abandoned preview
        # must not swap the perimeter under other users.
        try:
            perim_meta, perim_warnings = await run_in_threadpool(
                perimeter_mod.parse_and_cache, perim_data, perim_name)
        except ValueError as e:
            return templates.TemplateResponse(
                request, "shared/error.html", {"message": _td(request)(str(e))},
                status_code=422)
        except Exception as e:
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "Could not read the perimeter file: {e}", e=e)},
                status_code=422)
    else:
        # no upload in this request — fall back to the promoted default
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
        "remap": remap or None,
    }
    db.run_create(run_id, plog_path=str(plog_path), dmr_path=str(dmr_path),
                  plog_name=plog_name, dmr_name=dmr_name,
                  preview=preview,
                  perimeter_hash=perim_meta["hash"] if perim_meta else None)
    return templates.TemplateResponse(request, "reconciler/preview.html", {
        "run_id": run_id, "preview": preview,
        "tikhub_configured": bool(config.TIKHUB_API_KEY),
        "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
    })


@router.post("/upload", response_class=HTMLResponse)
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
    perim_data = None
    perim_name = ""
    if perimeter is not None and perimeter.filename:
        perim_data = await perimeter.read()
        perim_name = Path(perimeter.filename).name

    paths = {"plog": plog_path, "dmr": dmr_path}
    remap: dict = {}
    audits: dict = {}
    fail_msgs: dict = {}
    for kind in ("plog", "dmr"):
        try:
            # openpyxl parsing is CPU-bound; keep it off the event loop so
            # /healthz and progress polls stay responsive during big uploads.
            await run_in_threadpool(_parser_of(kind), str(paths[kind]))
        except ValueError as e:
            # unfamiliar headers — cached approved mapping, LLM proposal for
            # the audit screen, or (mapper unavailable) the plain error
            outcome = await run_in_threadpool(
                attempt_remap, kind, paths[kind].read_bytes())
            if outcome.status == "cached":
                mapping = outcome.mapping
                remapped = mapper.apply_mapping(
                    paths[kind].read_bytes(), kind, mapping["sheet"],
                    int(mapping["header_row"]),
                    {k: int(v) for k, v in mapping["columns"].items()})
                new_path = paths[kind].with_name("remapped_" + paths[kind].name)
                new_path.write_bytes(remapped)
                paths[kind] = new_path
                remap[kind] = remap_note(mapping, kind, auto=True)
            elif outcome.status == "audit":
                audits[kind] = {"proposal": outcome.proposal.model_dump(),
                                "choices": outcome.choices, "sig": outcome.sig}
            else:
                fail_msgs[kind] = (str(e), outcome.error)
        except Exception as e:  # corrupt zip, wrong format, … — never a 500
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "Could not read the uploaded file(s) as .xlsx: {e}", e=e)},
                status_code=422)

    if fail_msgs:
        parse_err, map_err = next(iter(fail_msgs.values()))
        msg = _td(request)(parse_err)
        if map_err:
            msg += f" ({_tr(request)('Header mapping also failed: {e}', e=map_err)})"
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": msg}, status_code=422)

    if audits:
        token = PENDING_MAPS.put({
            "flow": "run", "run_id": run_id, "run_dir": str(run_dir),
            "paths": {k: str(v) for k, v in paths.items()},
            "names": {"plog": plog.filename or "plog.xlsx",
                      "dmr": dmr.filename or "dmr.xlsx"},
            "perim_data": perim_data, "perim_name": perim_name,
            "audits": audits, "remap": remap,
        })
        return RedirectResponse(f"/remap/{token}", status_code=303)

    return await _finish_upload(
        request, run_id, run_dir, paths["plog"], paths["dmr"],
        plog.filename or "plog.xlsx", dmr.filename or "dmr.xlsx",
        perim_data, perim_name, remap)


async def _apply_remap_run(request: Request, token: str, entry: dict,
                           approved: dict, username: str):
    """Continuation of POST /remap/{token}/apply for the reconciler flow:
    write remapped copies next to the originals, then continue."""
    remap = dict(entry.get("remap") or {})
    paths = {k: Path(v) for k, v in entry["paths"].items()}
    for kind, m in approved.items():
        src = paths[kind]
        remapped = mapper.apply_mapping(
            src.read_bytes(), kind, m["sheet"], m["header_row"], m["columns"])
        new_path = src.with_name("remapped_" + src.name)
        new_path.write_bytes(remapped)
        paths[kind] = new_path
        mapper.cache_put(kind, m["sig"], m["sheet"], m["header_row"],
                         m["columns"], username)
        remap[kind] = remap_note(
            {**m, "approved_by": username}, kind, auto=False)
    PENDING_MAPS.pop(token)
    return await _finish_upload(
        request, entry["run_id"], Path(entry["run_dir"]),
        paths["plog"], paths["dmr"],
        entry["names"]["plog"], entry["names"]["dmr"],
        entry.get("perim_data"), entry.get("perim_name", ""), remap)


FLOW_HANDLERS["run"] = _apply_remap_run


# -------------------------------------------------------------------- runs

@router.post("/runs/{run_id}/start")
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
            if run.get("perimeter_hash"):
                # run confirmation is the moment this perimeter becomes the
                # app-wide default (see perimeter.parse_and_cache)
                perimeter_mod.promote_cached(run["perimeter_hash"])
            runs.start_run(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str):
    run = db.run_get(run_id)
    if not run:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("Run not found")},
            status_code=404)
    return templates.TemplateResponse(request, "reconciler/run.html", {
        "run": run, "run_id": run_id,
        "preview": json.loads(run.get("preview_json") or "{}"),
    })


@router.get("/runs/{run_id}/progress", response_class=HTMLResponse)
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
            request, "reconciler/_error_panel.html",
            {"run": run, "run_id": run_id,
             "options": json.loads(run.get("options_json") or "{}")})
        resp.headers["HX-Retarget"] = "#run-body"
        return resp
    return templates.TemplateResponse(request, "reconciler/_progress.html",
                                      {"run": run})


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
    return templates.TemplateResponse(request, "reconciler/_results.html", {
        "run": run, "run_id": run_id, "verdicts": verdicts,
        "counts": result.get("counts", {}),
        "buckets": result.get("buckets"),
        "perimeter_meta": result.get("perimeter_meta"),
        "perimeter_warning": result.get("perimeter_warning"),
        "reverse_rows": result.get("reverse_audit", []),
        "plog_meta": result.get("plog_meta", {}),
        "dmr_meta": result.get("dmr_meta", {}),
        "summary": summary,
        "engagement_caveat": ENGAGEMENT_CAVEAT,
        "badges": STATUS_BADGES, "override_choices": OVERRIDE_CHOICES,
    })


@router.get("/runs/{run_id}/results", response_class=HTMLResponse)
async def results(request: Request, run_id: str):
    return await results_fragment(request, run_id)


@router.post("/runs/{run_id}/override", response_class=HTMLResponse)
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


@router.get("/runs/{run_id}/export.xlsx")
async def export_xlsx(run_id: str):
    run = _run_or_404(run_id)
    if not run:
        return Response("run not finished", status_code=404)
    result = json.loads(run["result_json"])
    verdicts = load_verdicts(run)
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


@router.get("/runs/{run_id}/export.json")
async def export_json(run_id: str):
    run = _run_or_404(run_id)
    if not run:
        return Response("run not finished", status_code=404)
    result = json.loads(run["result_json"])
    verdicts = load_verdicts(run)
    overrides = db.overrides_for_run(run_id)
    doc = build_audit_json(run, verdicts, result.get("counts", {}),
                           result.get("plog_meta", {}),
                           result.get("dmr_meta", {}),
                           result.get("reverse_audit", []),
                           overrides=overrides)
    return Response(doc, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="audit_{run_id}.json"'})


@router.get("/runs/{run_id}/api")
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
