"""KOL efficiency report web flow.

Client campaign data: the uploaded workbook is analyzed straight from
memory and never written to disk or the database. Finished reports live in
a small in-process store (TTL + cap) just long enough to be downloaded.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
import re
from typing import Optional
from urllib.parse import quote
import zipfile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .. import config
from ..core.token_store import TokenStore, TokenStoreFull
from ..core.uploads import (UploadLimitError, read_limited,
                            run_upload_task, validate_xlsx_archive)
from ..remap import mapper
from ..remap.routes import FLOW_HANDLERS
from ..remap.service import (PENDING_MAPS, finish_remap, inspect_remap,
                             remap_note)
from ..web import templates, td as _td, tr as _tr
from .analysis import (COOPS, FANBASE_UNITS, TIERS, ReportConfig,
                       VerificationError,
                       analyze as analyze_efficiency,
                       probe_report_schema)
from .deck import DONUT_COLORS, assert_chart_cache, build_deck

router = APIRouter()
logger = logging.getLogger(__name__)

EFF_REPORTS = TokenStore(ttl_seconds=2 * 3600, max_entries=20)

EFF_BASES = {"pooled", "per_post"}
EFF_TIER_MODES = {"label", "fanbase"}
EFF_FANBASE_UNITS = FANBASE_UNITS
EFF_LANGUAGES = {"en", "zh"}
EXPECTED_WORKBOOK_ERRORS = (
    zipfile.BadZipFile,
    InvalidFileException,
    ElementTree.ParseError,
    DefusedXmlException,
    XMLSyntaxError,
    KeyError,
    IndexError,
)
CACHED_MAPPING_ERRORS = EXPECTED_WORKBOOK_ERRORS + (
    mapper.SchemaMapError,
    TypeError,
    ValueError,
)


class _EfficiencyMultiPartParser(MultiPartParser):
    """Keep the single efficiency workbook in memory for this route only.

    Starlette's global 1 MiB threshold otherwise rolls ordinary client XLSX
    files to an OS temporary file before the endpoint can read them.  The
    request-body middleware still bounds the whole request, and ``read_limited``
    enforces the exact workbook limit after parsing.  Reconciler uploads keep
    Starlette's default spool policy.
    """

    spool_max_size = config.MAX_UPLOAD_BYTES + 1024 * 1024


def _form_text(form, name: str, default: str) -> str:
    value = form.get(name, default)
    return value if isinstance(value, str) else default


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
        "remap": entry.get("remap"),
    }


@router.get("/efficiency", response_class=HTMLResponse)
async def efficiency_form(request: Request):
    return templates.TemplateResponse(request, "efficiency/efficiency.html", {})


async def _efficiency_analyze(request: Request, data: bytes, filename: str,
                              cfg_raw: dict, remap_note_dict: Optional[dict],
                              approved_cache: Optional[
                                  list[tuple[str, dict]]] = None):
    """Analyze → build deck → store → redirect. Shared by the direct upload
    path and the post-audit remap path (everything stays in memory)."""
    cfg = ReportConfig(**cfg_raw)

    try:
        analysis = await run_upload_task(
            request, analyze_efficiency, io.BytesIO(data), cfg)
    except UploadLimitError as e:
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _tr(request)(str(e))},
            status_code=413)
    except ValueError as e:  # V1 — wrong sheet shape
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _td(request)(str(e))},
            status_code=422)
    except VerificationError:
        logger.exception("efficiency report cross-check failed")
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Internal cross-check failed — report not generated.")},
            status_code=500)
    except Exception:
        logger.exception("unexpected efficiency report failure")
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Unexpected server error — report not generated.")},
            status_code=500)

    pptx = None
    if not analysis["blocked"]:
        def _build_and_verify():
            deck = build_deck(analysis)
            assert_chart_cache(deck, analysis)
            return deck

        try:
            pptx = await run_upload_task(request, _build_and_verify)
        except UploadLimitError as e:
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(str(e))}, status_code=413)
        except VerificationError:
            logger.exception("efficiency deck cross-check failed")
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "Internal cross-check failed — report not generated.")},
                status_code=500)
        except Exception:
            logger.exception("unexpected efficiency deck failure")
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(
                    "Unexpected server error — report not generated.")},
                status_code=500)

    # Human approval becomes a shared auto-map only after the deterministic
    # analysis is unblocked and the generated deck has passed its independent
    # chart-cache verification. A diagnostic-only (blocked) report is useful
    # to the uploader but is not evidence that the mapping is safe to reuse.
    mapping_promoted = bool(
        approved_cache and not analysis["blocked"] and pptx is not None)
    if mapping_promoted:
        for kind, mapping in approved_cache:
            mapper.cache_put(
                kind,
                mapping["sig"],
                mapping["sheet"],
                mapping["header_row"],
                mapping["columns"],
                mapping["approved_by"],
            )
    if remap_note_dict is not None:
        remap_note_dict["shared"] = bool(
            remap_note_dict.get("auto") or mapping_promoted)

    try:
        token = EFF_REPORTS.put({
            "analysis": analysis,
            "pptx": pptx,
            "filename": filename,
            "remap": remap_note_dict,
        })
    except TokenStoreFull:
        return templates.TemplateResponse(
            request,
            "shared/error.html",
            {"message": _tr(request)(
                "Too many efficiency reports are active. Try again shortly.")},
            status_code=503,
            headers={"Retry-After": "60"},
        )
    return RedirectResponse(f"/efficiency/{token}", status_code=303)


@router.post("/efficiency", response_class=HTMLResponse)
async def efficiency_run(request: Request):
    """Parse this privacy-sensitive multipart request without disk spooling."""
    form = None
    try:
        if not request.headers.get("content-type", "").lower().startswith(
                "multipart/form-data"):
            return templates.TemplateResponse(
                request,
                "shared/error.html",
                {"message": _tr(request)("Could not parse the upload form.")},
                status_code=400,
            )
        parser = _EfficiencyMultiPartParser(
            request.headers,
            request.stream(),
            max_files=1,
            max_fields=8,
            max_part_size=8 * 1024,
        )
        # Config is mutable in tests and may be reloaded by an embedding app;
        # keep the instance threshold above the active request limit too.
        parser.spool_max_size = config.MAX_UPLOAD_BYTES + 1024 * 1024
        form = await parser.parse()
        report = form.get("report")
        if not isinstance(report, UploadFile):
            return templates.TemplateResponse(
                request,
                "shared/error.html",
                {"message": _tr(request)("An .xlsx report file is required.")},
                status_code=422,
            )
        return await _efficiency_run_upload(
            request,
            report,
            basis=_form_text(form, "basis", "pooled"),
            tier_mode=_form_text(form, "tier_mode", "label"),
            fanbase_unit=_form_text(form, "fanbase_unit", "k"),
            language=_form_text(form, "language", "en"),
            allow_header_ai=_form_text(form, "allow_header_ai", "0"),
        )
    except MultiPartException:
        return templates.TemplateResponse(
            request,
            "shared/error.html",
            {"message": _tr(request)("Could not parse the upload form.")},
            status_code=400,
        )
    finally:
        if form is not None:
            await form.close()


async def _efficiency_run_upload(request: Request, report: UploadFile,
                                 basis: str, tier_mode: str, fanbase_unit: str,
                                 language: str,
                                 allow_header_ai: str):
    cfg_raw = {
        "basis": basis if basis in EFF_BASES else "pooled",
        "tier_mode": tier_mode if tier_mode in EFF_TIER_MODES else "label",
        "fanbase_unit": (
            fanbase_unit if fanbase_unit in EFF_FANBASE_UNITS else "k"),
        "language": language if language in EFF_LANGUAGES else "en",
    }
    try:
        data = await read_limited(report, config.MAX_UPLOAD_BYTES)
        await run_upload_task(
            request,
            validate_xlsx_archive,
            data,
            max_uncompressed_bytes=config.MAX_XLSX_UNCOMPRESSED_BYTES,
            max_entries=config.MAX_XLSX_ENTRIES,
            max_cells=config.MAX_XLSX_CELLS,
            max_sheets=config.MAX_XLSX_SHEETS,
            max_row_index=config.MAX_XLSX_ROW_INDEX,
            max_column_index=config.MAX_XLSX_COLUMN_INDEX,
        )
    except UploadLimitError as e:
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _tr(request)(str(e))},
            status_code=413)
    except EXPECTED_WORKBOOK_ERRORS:
        return templates.TemplateResponse(
            request,
            "shared/error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file as .xlsx.")},
            status_code=422,
        )
    except Exception:
        logger.exception("unexpected efficiency upload validation failure")
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("Unexpected server error.")},
            status_code=500)
    filename = Path(report.filename or "report.xlsx").stem

    # A schema-only probe avoids fully parsing every valid workbook twice.
    try:
        await run_upload_task(request, probe_report_schema, io.BytesIO(data))
    except UploadLimitError as e:
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _tr(request)(str(e))},
            status_code=413)
    except EXPECTED_WORKBOOK_ERRORS:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file as .xlsx.")},
            status_code=422)
    except ValueError as e:
        try:
            outcome = await run_upload_task(request, inspect_remap, "eff", data)
            if outcome.status == "cached":
                mapping = outcome.mapping
                try:
                    remapped = await run_upload_task(
                        request,
                        mapper.apply_mapping,
                        data,
                        "eff",
                        mapping["sheet"],
                        int(mapping["header_row"]),
                        {key: int(value)
                         for key, value in mapping["columns"].items()},
                    )
                    # Applying a cached map is trusted only if the resulting
                    # workbook now exposes the deterministic schema. This is
                    # the only post-apply failure attributable to the shared
                    # mapping itself; row/content validation below must not
                    # let an ordinary uploader revoke a valid global map.
                    await run_upload_task(
                        request,
                        probe_report_schema,
                        io.BytesIO(remapped),
                    )
                except CACHED_MAPPING_ERRORS:
                    mapper.cache_delete("eff", mapping["sig"])
                    logger.info(
                        "revoked cached efficiency mapping after apply failure",
                        exc_info=True,
                    )
                    return templates.TemplateResponse(
                        request,
                        "shared/error.html",
                        {"message": _tr(request)(
                            "The saved header mapping was invalid and has "
                            "been revoked. Upload the workbook again to "
                            "review a new mapping.")},
                        status_code=422,
                    )
                response = await _efficiency_analyze(
                    request, remapped, filename, cfg_raw,
                    remap_note(mapping, "eff", auto=True))
                return response
            if outcome.status == "ready":
                if allow_header_ai != "1":
                    return templates.TemplateResponse(
                        request, "shared/error.html",
                        {"message": _tr(request)(
                            "The headers are unfamiliar. No workbook sample "
                            "was sent externally; enable the disclosed Claude "
                            "header-mapping option and upload again.")},
                        status_code=422)
                try:
                    proposal = await run_in_threadpool(
                        mapper.propose, outcome.sample or {}, "eff")
                except mapper.SchemaMapError as mapping_error:
                    outcome.error = str(mapping_error)
                    outcome.status = "fail"
                else:
                    outcome = await run_upload_task(
                        request, finish_remap, "eff", data, proposal)
            if outcome.status == "audit":
                try:
                    token = PENDING_MAPS.put({
                        "flow": "eff", "data": data, "filename": filename,
                        "cfg": cfg_raw,
                        "audits": {"eff": {
                            "proposal": outcome.proposal.model_dump(),
                            "choices": outcome.choices,
                            "sig": outcome.sig,
                        }},
                    })
                except TokenStoreFull:
                    return templates.TemplateResponse(
                        request, "shared/error.html",
                        {"message": _tr(request)(
                            "Too many mapping audits are active. Try again shortly.")},
                        status_code=503,
                    )
                return RedirectResponse(f"/remap/{token}", status_code=303)
            msg = _td(request)(str(e))
            if outcome.error:
                msg += f" ({_tr(request)('Header mapping also failed: {e}', e=outcome.error)})"
            return templates.TemplateResponse(
                request, "shared/error.html", {"message": msg},
                status_code=422)
        except UploadLimitError as limit_error:
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)(str(limit_error))},
                status_code=413)
        except Exception:
            logger.exception("unexpected efficiency header-mapping failure")
            return templates.TemplateResponse(
                request, "shared/error.html",
                {"message": _tr(request)("Unexpected server error.")},
                status_code=500)
    except Exception:
        logger.exception("efficiency schema probe failed unexpectedly")
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("Unexpected server error.")},
            status_code=500)

    return await _efficiency_analyze(request, data, filename, cfg_raw, None)


async def _apply_remap_eff(request: Request, token: str, entry: dict,
                           approved: dict, username: str):
    """Continuation of POST /remap/{token}/apply for the efficiency flow —
    everything stays in memory."""
    remap = dict(entry.get("remap") or {})
    data = entry["data"]
    pending_cache: list[tuple[str, dict]] = []
    for kind, m in approved.items():   # single "eff" entry
        data = await run_upload_task(
            request,
            mapper.apply_mapping,
            data,
            kind,
            m["sheet"],
            m["header_row"],
            m["columns"],
        )
        pending_cache.append((kind, m))
        remap[kind] = remap_note(
            {**m, "approved_by": username}, kind, auto=False)
    response = await _efficiency_analyze(
        request,
        data,
        entry["filename"],
        entry["cfg"],
        remap.get("eff"),
        approved_cache=[
            (kind, {**mapping, "approved_by": username})
            for kind, mapping in pending_cache
        ],
    )
    return response


FLOW_HANDLERS["eff"] = _apply_remap_eff


@router.get("/efficiency/{token}", response_class=HTMLResponse)
async def efficiency_report(request: Request, token: str):
    entry = EFF_REPORTS.get(token)
    if not entry:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "This report has expired (reports are kept in memory for 2 "
                "hours, never stored). Re-upload the workbook.")},
            status_code=404)
    return templates.TemplateResponse(request,
                                      "efficiency/efficiency_report.html",
                                      _eff_report_context(token, entry))


@router.get("/efficiency/{token}/deck.pptx")
async def efficiency_deck(token: str):
    entry = EFF_REPORTS.get(token)
    if not entry or entry.get("pptx") is None:
        return Response("report expired or deck blocked by validation",
                        status_code=404)
    fname = f"{entry['filename']}_efficiency.pptx"
    # Multipart filenames are attacker-controlled. Keep Unicode for RFC 5987
    # but remove controls/path separators; use an even stricter ASCII fallback
    # so no raw CR/LF can reach the legacy Content-Disposition parameter.
    fname = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", fname)
    # RFC 6266/5987: response headers are Latin-1, and this product's users
    # upload Chinese-named workbooks — a raw filename= raised
    # UnicodeEncodeError (500). Modern browsers take filename*; the ASCII
    # fallback is for anything ancient.
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", fname).strip("._")
    fallback = re.sub(r"_+", "_", fallback)
    fallback = fallback or "efficiency_report.pptx"
    return Response(
        entry["pptx"],
        media_type="application/vnd.openxmlformats-officedocument"
                   ".presentationml.presentation",
        headers={"Content-Disposition":
                 f'attachment; filename="{fallback}"; '
                 f"filename*=UTF-8''{quote(fname, safe='')}"})
