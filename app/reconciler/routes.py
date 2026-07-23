"""Reconciler web flow: dashboard → upload → preview → run → results →
overrides → exports. Business logic lives in the sibling modules; handlers
orchestrate and render."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Optional
from zipfile import BadZipFile

from defusedxml.ElementTree import ParseError
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException
from starlette.concurrency import run_in_threadpool

from .. import config
from ..core import db
from ..core.token_store import TokenStoreFull
from ..core.uploads import (UploadLimitError, read_limited,
                            register_active_upload, remove_tree,
                            run_upload_task, save_limited,
                            unregister_active_upload, validate_xlsx_archive)
from ..remap import mapper
from ..remap.routes import FLOW_HANDLERS
from ..remap.service import (PENDING_MAPS, RemapOutcome, finish_remap,
                             inspect_remap, remap_note)
from ..web import current_user, templates, td as _td, tr as _tr
from . import perimeter as perimeter_mod, runs
from .domain import (ENGAGEMENT_CAVEAT, OVERRIDE_CHOICES,
                     effective_verdict_dict)
from .export import build_audit_json, load_verdicts, write_annotated_xlsx
from .parsers import (parse_dmr, parse_plog, probe_dmr_schema,
                      probe_plog_schema)
from .pipeline import status_counts, summary_buckets
from .presentation import STATUS_BADGES

router = APIRouter()
logger = logging.getLogger(__name__)
EXPECTED_WORKBOOK_ERRORS = (BadZipFile, InvalidFileException, ParseError,
                            DefusedXmlException, XMLSyntaxError)

_start_lock = threading.Lock()
_export_stream_slots = threading.BoundedSemaphore(
    config.EXPORT_STREAM_CONCURRENCY
)


class _LeasedFileResponse(FileResponse):
    """Keep a run directory protected through response streaming."""

    def __init__(self, *args, lease_path: Path, cleanup_path: Path,
                 stream_slot: threading.BoundedSemaphore, **kwargs):
        self.lease_path = lease_path
        self.cleanup_path = cleanup_path
        self.stream_slot = stream_slot
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.cleanup_path.unlink(missing_ok=True)
            except OSError:
                pass
            finally:
                try:
                    unregister_active_upload(self.lease_path)
                finally:
                    self.stream_slot.release()


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
async def perimeter_remove(request: Request):
    user = current_user(request)
    if config.APP_PASSWORD and (not user or not user.get("is_admin")):
        return Response("administrator required", status_code=403)
    db.setting_delete("current_perimeter")
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ upload

def _parser_of(kind: str):
    return parse_plog if kind == "plog" else parse_dmr


def _probe_of(kind: str):
    return probe_plog_schema if kind == "plog" else probe_dmr_schema


def _remap_file(source: Path, kind: str, mapping: dict) -> Path:
    """Apply one approved mapping without blocking the event loop."""
    remapped = mapper.apply_mapping(
        source.read_bytes(),
        kind,
        mapping["sheet"],
        int(mapping["header_row"]),
        {key: int(value) for key, value in mapping["columns"].items()},
    )
    destination = source.with_name("remapped_" + source.name)
    destination.write_bytes(remapped)
    return destination


async def _validate_workbook(request: Request, source) -> None:
    await run_upload_task(
        request,
        validate_xlsx_archive,
        source,
        max_uncompressed_bytes=config.MAX_XLSX_UNCOMPRESSED_BYTES,
        max_entries=config.MAX_XLSX_ENTRIES,
        max_cells=config.MAX_XLSX_CELLS,
        max_sheets=config.MAX_XLSX_SHEETS,
        max_row_index=config.MAX_XLSX_ROW_INDEX,
        max_column_index=config.MAX_XLSX_COLUMN_INDEX,
    )


async def _finish_upload(request: Request, run_id: str, run_dir: Path,
                         plog_path: Path, dmr_path: Path,
                         plog_name: str, dmr_name: str,
                         perim_data: Optional[bytes], perim_name: str,
                         remap: dict, parsed: Optional[dict] = None):
    """Everything after both workbooks parse: perimeter ingest, preview,
    run row, preview page. `remap` records any header mappings applied."""
    parsed = parsed or {}
    try:
        p = (parsed.get("plog") or
             await run_upload_task(request, parse_plog, str(plog_path)))
        d = (parsed.get("dmr") or
             await run_upload_task(request, parse_dmr, str(dmr_path)))
    except ValueError as e:
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _td(request)(str(e))},
            status_code=422)
    except EXPECTED_WORKBOOK_ERRORS:
        logger.info("invalid workbook structure for run %s", run_id, exc_info=True)
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("The uploaded file is not a valid .xlsx workbook.")},
            status_code=422)
    except Exception:
        logger.exception("unexpected workbook parse failure for run %s", run_id)
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("An internal workbook parsing error occurred.")},
            status_code=500)
    except BaseException:
        remove_tree(run_dir)
        raise

    perim_warnings: list[str] = []
    perimeter_uploaded = perim_data is not None
    if perimeter_uploaded:
        # Parse + cache only. The upload becomes the app-wide current
        # perimeter when this run is actually STARTED — an abandoned preview
        # must not swap the perimeter under other users.
        try:
            perim_meta, perim_warnings = await run_upload_task(
                request, perimeter_mod.parse_and_cache,
                perim_data, perim_name)
        except db.StorageLimitError:
            logger.info("perimeter cache limit rejected run %s", run_id,
                        exc_info=True)
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "The perimeter is too large for the configured storage limit.")},
                status_code=413)
        except UploadLimitError as e:
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html", {"message": _tr(request)(str(e))},
                status_code=413)
        except ValueError as e:
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html", {"message": _td(request)(str(e))},
                status_code=422)
        except EXPECTED_WORKBOOK_ERRORS:
            logger.info("invalid perimeter workbook for run %s", run_id,
                        exc_info=True)
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)("The perimeter file is not a valid .xlsx workbook.")},
                status_code=422)
        except Exception:
            logger.exception("unexpected perimeter parse failure for run %s", run_id)
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)("An internal perimeter parsing error occurred.")},
                status_code=500)
        except BaseException:
            remove_tree(run_dir)
            raise
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
    try:
        # Render before committing the durable row. A template failure after
        # the insert would otherwise leave a retryable remap token pointing at
        # a run that already exists, and its retry cleanup could orphan it.
        response = templates.TemplateResponse(
            request,
            "reconciler/preview.html",
            {
                "run_id": run_id,
                "preview": preview,
                "tikhub_configured": bool(config.TIKHUB_API_KEY),
                "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
            },
        )
        db.run_create(run_id, plog_path=str(plog_path), dmr_path=str(dmr_path),
                      plog_name=plog_name, dmr_name=dmr_name,
                      preview=preview,
                      perimeter_hash=perim_meta["hash"] if perim_meta else None,
                      perimeter_uploaded=perimeter_uploaded,
                      perimeter_name=(perim_meta.get("filename")
                                      if perim_meta else None))
    except db.StorageLimitError:
        logger.info("run preview exceeded storage limit", exc_info=True)
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "The upload exceeds the configured storage limit.")},
            status_code=413,
        )
    except BaseException:
        remove_tree(run_dir)
        raise
    return response


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, plog: UploadFile, dmr: UploadFile,
                 perimeter: Optional[UploadFile] = File(None),
                 allow_header_ai: str = Form("0")):
    if (perimeter is not None and perimeter.filename and config.APP_PASSWORD):
        user = current_user(request)
        if not user or not user.get("is_admin"):
            return Response("administrator required to replace perimeter",
                            status_code=403)
    config.ensure_dirs()
    while True:
        run_id = uuid.uuid4().hex[:12]
        run_dir = config.UPLOAD_DIR / run_id
        if not register_active_upload(run_dir):
            continue
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            unregister_active_upload(run_dir)
            continue
        except BaseException:
            unregister_active_upload(run_dir)
            raise
        break
    try:
        return await _process_upload(
            request, plog, dmr, perimeter, run_id, run_dir,
            allow_header_ai == "1",
        )
    finally:
        unregister_active_upload(run_dir)


async def _process_upload(request: Request, plog: UploadFile, dmr: UploadFile,
                          perimeter: Optional[UploadFile], run_id: str,
                          run_dir: Path, allow_header_ai: bool = False):
    plog_path = run_dir / ("plog_" + Path(plog.filename or "plog.xlsx").name)
    dmr_path = run_dir / ("dmr_" + Path(dmr.filename or "dmr.xlsx").name)
    perim_data = None
    perim_name = ""
    try:
        await save_limited(plog, plog_path, config.MAX_UPLOAD_BYTES)
        await save_limited(dmr, dmr_path, config.MAX_UPLOAD_BYTES)
        await _validate_workbook(request, str(plog_path))
        await _validate_workbook(request, str(dmr_path))
        if perimeter is not None and perimeter.filename:
            perim_data = await read_limited(perimeter, config.MAX_UPLOAD_BYTES)
            perim_name = Path(perimeter.filename).name
            await _validate_workbook(request, perim_data)
    except UploadLimitError as e:
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _tr(request)(str(e))},
            status_code=413)
    except Exception:
        logger.info("uploaded workbook archive validation failed", exc_info=True)
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file(s) as a valid .xlsx workbook.")},
            status_code=422)
    except BaseException:
        remove_tree(run_dir)
        raise

    paths = {"plog": plog_path, "dmr": dmr_path}
    remap: dict = {}
    parsed_results: dict = {}
    audits: dict = {}
    fail_msgs: dict = {}
    for kind in ("plog", "dmr"):
        try:
            # openpyxl parsing is CPU-bound; keep it off the event loop so
            # /healthz and progress polls stay responsive during big uploads.
            await run_upload_task(
                request, _probe_of(kind), str(paths[kind])
            )
        except ValueError as e:
            # unfamiliar headers — cached approved mapping, LLM proposal for
            # the audit screen, or (mapper unavailable) the plain error
            try:
                source_data = await run_upload_task(
                    request, paths[kind].read_bytes
                )
                outcome = await run_upload_task(
                    request, inspect_remap, kind, source_data
                )
                if outcome.status == "ready":
                    if not allow_header_ai:
                        outcome = RemapOutcome(
                            "fail",
                            error=(
                                "Headers are unfamiliar. No workbook sample was "
                                "sent to Claude because AI header mapping was not "
                                "explicitly allowed on the upload form."
                            ),
                        )
                    else:
                        try:
                            proposal = await run_in_threadpool(
                                mapper.propose, outcome.sample or {}, kind
                            )
                            outcome = await run_upload_task(
                                request, finish_remap, kind, source_data, proposal
                            )
                        except mapper.SchemaMapError as remap_error:
                            outcome = RemapOutcome("fail", error=str(remap_error))
                if outcome.status == "cached":
                    mapping = outcome.mapping
                    try:
                        paths[kind] = await run_upload_task(
                            request, _remap_file, paths[kind], kind, mapping
                        )
                        # A cached mapping is trusted only while its rewritten
                        # workbook still satisfies the deterministic parser.
                        # Layout drift can preserve the signature while making
                        # the old semantic choices invalid.
                        parsed_results[kind] = await run_upload_task(
                            request, _parser_of(kind), str(paths[kind])
                        )
                        remap[kind] = remap_note(mapping, kind, auto=True)
                    except (ValueError, mapper.SchemaMapError) as mapping_error:
                        if mapping and mapping.get("sig"):
                            mapper.cache_delete(kind, mapping["sig"])
                        fail_msgs[kind] = (str(e), str(mapping_error))
                elif outcome.status == "audit":
                    audits[kind] = {
                        "proposal": outcome.proposal.model_dump(),
                        "choices": outcome.choices,
                        "sig": outcome.sig,
                    }
                else:
                    fail_msgs[kind] = (str(e), outcome.error)
            except Exception:
                logger.exception("unexpected header remapping failure for %s", kind)
                remove_tree(run_dir)
                return templates.TemplateResponse(
                    request,
                    "shared/error.html",
                    {"message": _tr(request)(
                        "An internal header-mapping error occurred.")},
                    status_code=500,
                )
            except BaseException:
                remove_tree(run_dir)
                raise
        except EXPECTED_WORKBOOK_ERRORS:
            logger.info("invalid workbook structure during upload for %s", kind,
                        exc_info=True)
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)("The uploaded file is not a valid .xlsx workbook.")},
                status_code=422)
        except Exception:
            logger.exception("unexpected parser failure during upload for %s", kind)
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)("An internal workbook parsing error occurred.")},
                status_code=500)
        except BaseException:
            remove_tree(run_dir)
            raise

    if fail_msgs:
        parse_err, map_err = next(iter(fail_msgs.values()))
        msg = _td(request)(parse_err)
        if map_err:
            msg += f" ({_tr(request)('Header mapping also failed: {e}', e=map_err)})"
        remove_tree(run_dir)
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": msg}, status_code=422)

    if audits:
        try:
            token = PENDING_MAPS.put({
                "flow": "run", "run_id": run_id,
                "run_dir": str(run_dir),
                "paths": {k: str(v) for k, v in paths.items()},
                "names": {"plog": plog.filename or "plog.xlsx",
                          "dmr": dmr.filename or "dmr.xlsx"},
                "perim_data": perim_data, "perim_name": perim_name,
                "audits": audits, "remap": remap,
            })
        except TokenStoreFull:
            remove_tree(run_dir)
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "Too many mapping audits are active. Try again shortly.")},
                status_code=503,
            )
        return RedirectResponse(f"/remap/{token}", status_code=303)

    return await _finish_upload(
        request, run_id, run_dir, paths["plog"], paths["dmr"],
        plog.filename or "plog.xlsx", dmr.filename or "dmr.xlsx",
        perim_data, perim_name, remap, parsed=parsed_results)


async def _apply_remap_run(request: Request, token: str, entry: dict,
                           approved: dict, username: str):
    """Continuation of POST /remap/{token}/apply for the reconciler flow:
    write remapped copies next to the originals, then continue."""
    remap = dict(entry.get("remap") or {})
    paths = {k: Path(v) for k, v in entry["paths"].items()}
    parsed_results: dict = {}
    for kind, m in approved.items():
        src = paths[kind]
        paths[kind] = await run_upload_task(
            request, _remap_file, src, kind, m
        )
        parsed_results[kind] = await run_upload_task(
            request, _parser_of(kind), str(paths[kind])
        )
        mapper.cache_put(kind, m["sig"], m["sheet"], m["header_row"],
                         m["columns"], username)
        remap[kind] = remap_note(
            {**m, "approved_by": username}, kind, auto=False, shared=True)
    return await _finish_upload(
        request, entry["run_id"], Path(entry["run_dir"]),
        paths["plog"], paths["dmr"],
        entry["names"]["plog"], entry["names"]["dmr"],
        entry.get("perim_data"), entry.get("perim_name", ""), remap,
        parsed=parsed_results)


FLOW_HANDLERS["run"] = _apply_remap_run


# -------------------------------------------------------------------- runs

@router.post("/runs/{run_id}/start")
async def start(request: Request, run_id: str,
                retry_failed_links: str = Form("0"),
                use_llm: str = Form("0")):
    """Checkbox values arrive as "1" (hidden-input fallback supplies "0" when
    unchecked — a bool Form default can never receive False from a form)."""
    lease_path = config.UPLOAD_DIR / run_id
    if not register_active_upload(lease_path):
        return Response(status_code=404)
    try:
        with _start_lock:  # two concurrent POSTs cannot spawn duplicate work
            run = db.run_get(run_id)
            if not run:
                return Response(status_code=404)
            if run["status"] in ("pending", "error"):
                initial_start = run["status"] == "pending"
                if (initial_start and run.get("perimeter_uploaded") != 0
                        and run.get("perimeter_hash") and config.APP_PASSWORD):
                    user = current_user(request)
                    if not user or not user.get("is_admin"):
                        return Response(
                            "administrator required to promote perimeter",
                            status_code=403,
                        )
                if (run.get("perimeter_hash")
                        and db.perimeter_cache_get(run["perimeter_hash"]) is None):
                    return Response(
                        "the perimeter snapshot for this run is unavailable; "
                        "create a new run with a current perimeter",
                        status_code=409,
                    )
                db.run_update(run_id, options_json=json.dumps({
                    "retry_failed_links": retry_failed_links == "1",
                    "use_llm": use_llm == "1",
                }), status="queued", error=None)
                if (initial_start and run.get("perimeter_uploaded") != 0
                        and run.get("perimeter_hash")):
                    # Explicit uploads promote once. NULL denotes a migrated
                    # legacy row whose provenance was not recorded, so it
                    # retains the previous release's one-time behavior.
                    perimeter_mod.promote_cached(
                        run["perimeter_hash"],
                        filename=run.get("perimeter_name") or "")
                runs.start_run(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    finally:
        unregister_active_upload(lease_path)


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
    pipeline_verdicts = result.get("verdicts", [])
    overrides = db.overrides_for_run(run_id)
    verdicts = [
        effective_verdict_dict(v, overrides.get(v["excel_row"]))
        for v in pipeline_verdicts
    ]
    counts = status_counts(verdicts)
    buckets = summary_buckets(verdicts)
    summary = json.loads(run.get("summary_json") or "{}")
    return templates.TemplateResponse(request, "reconciler/_results.html", {
        "run": run, "run_id": run_id, "verdicts": verdicts,
        "counts": counts,
        "buckets": buckets,
        "perimeter_meta": result.get("perimeter_meta"),
        "perimeter_warning": result.get("perimeter_warning"),
        "reverse_rows": result.get("reverse_audit", []),
        "plog_meta": result.get("plog_meta", {}),
        "dmr_meta": result.get("dmr_meta", {}),
        "summary": summary,
        "has_overrides": bool(overrides),
        "engagement_caveat": ENGAGEMENT_CAVEAT,
        "badges": STATUS_BADGES, "override_choices": OVERRIDE_CHOICES,
    })


@router.get("/runs/{run_id}/results", response_class=HTMLResponse)
async def results(request: Request, run_id: str):
    return await results_fragment(request, run_id)


@router.post("/runs/{run_id}/override", response_class=HTMLResponse)
async def set_override(request: Request, run_id: str,
                       excel_row: int = Form(...),
                       status: str = Form(""), note: str = Form("")):
    run = db.run_get(run_id)
    if not run:
        return Response("run not found", status_code=404)
    result = json.loads(run.get("result_json") or "{}")
    verdict = next(
        (v for v in result.get("verdicts", [])
         if int(v.get("excel_row", -1)) == excel_row),
        None,
    )
    if verdict is None:
        return Response("verdict row not found", status_code=404)
    if status not in OVERRIDE_CHOICES:
        return Response("invalid override status", status_code=422)
    if len(note) > 2000:
        return Response("override note is too long", status_code=422)
    user = current_user(request)
    if status:
        # Campaign and NO are evidence from the immutable stored verdict; never
        # trust hidden fields supplied by the browser.
        db.override_set(run_id, excel_row, verdict.get("campaign", ""),
                        verdict.get("no", ""), status, note,
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
async def export_xlsx(request: Request, run_id: str):
    lease_path = config.UPLOAD_DIR / run_id
    if not _export_stream_slots.acquire(blocking=False):
        return Response(
            "too many exports in progress",
            status_code=503,
            headers={"Retry-After": "2"},
        )
    lease_registered = False
    handed_to_response = False
    out: Path | None = None
    try:
        if not register_active_upload(lease_path):
            return Response("run not finished", status_code=404)
        lease_registered = True
        run = _run_or_404(run_id)
        if not run:
            return Response("run not finished", status_code=404)
        result = json.loads(run["result_json"])
        verdicts = load_verdicts(run)
        overrides = db.overrides_for_run(run_id)
        # Each response owns a distinct inode until streaming completes. Two
        # overlapping exports must never overwrite the file already in flight.
        out = (Path(run["plog_path"]).parent
               / f".export-{run_id}-{uuid.uuid4().hex}.xlsx")
        await run_upload_task(
            request,
            write_annotated_xlsx,
            run["plog_path"], str(out), verdicts,
            result.get("plog_meta", {}).get("header_row", 1),
            result.get("plog_meta", {}).get("sheet"),
            overrides,
            result.get("perimeter_meta"),
            result.get("perimeter_warning"),
        )
        response = _LeasedFileResponse(
            str(out), filename=f"PLOG_DMR_CHECK_{run_id}.xlsx",
            lease_path=lease_path, cleanup_path=out,
            stream_slot=_export_stream_slots,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        handed_to_response = True
        return response
    finally:
        if not handed_to_response:
            try:
                if out is not None:
                    out.unlink(missing_ok=True)
            except OSError:
                pass
            finally:
                try:
                    if lease_registered:
                        unregister_active_upload(lease_path)
                finally:
                    _export_stream_slots.release()


@router.get("/runs/{run_id}/export.json")
async def export_json(run_id: str):
    lease_path = config.UPLOAD_DIR / run_id
    if not register_active_upload(lease_path):
        return Response("run not finished", status_code=404)
    try:
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
                               overrides=overrides,
                               perimeter_meta=result.get("perimeter_meta"),
                               perimeter_warning=result.get("perimeter_warning"))
        return Response(doc, media_type="application/json", headers={
            "Content-Disposition":
                f'attachment; filename="audit_{run_id}.json"'})
    finally:
        unregister_active_upload(lease_path)


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
