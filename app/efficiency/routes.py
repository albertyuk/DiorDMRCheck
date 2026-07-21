"""KOL efficiency report web flow.

Client campaign data: the uploaded workbook is analyzed straight from
memory and never written to disk or the database. Finished reports live in
a small in-process store (TTL + cap) just long enough to be downloaded.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import config
from ..core.token_store import TokenStore, TokenStoreFull
from ..core.uploads import (UploadLimitError, read_limited,
                            run_upload_task, validate_xlsx_archive)
from ..remap import mapper
from ..remap.routes import FLOW_HANDLERS
from ..remap.service import PENDING_MAPS, attempt_remap, remap_note
from ..web import templates, td as _td, tr as _tr
from .analysis import (COOPS, TIERS, ReportConfig, VerificationError,
                       analyze as analyze_efficiency,
                       parse_report as parse_eff_report)
from .deck import DONUT_COLORS, assert_chart_cache, build_deck

router = APIRouter()

EFF_REPORTS = TokenStore(ttl_seconds=2 * 3600, max_entries=20)

EFF_BASES = {"pooled", "per_post"}
EFF_TIER_MODES = {"label", "fanbase"}
EFF_LANGUAGES = {"en", "zh"}


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
                              cfg_raw: dict, remap_note_dict: Optional[dict]):
    """Analyze → build deck → store → redirect. Shared by the direct upload
    path and the post-audit remap path (everything stays in memory)."""
    cfg = ReportConfig(**cfg_raw)

    def _analyze_and_build():
        analysis = analyze_efficiency(io.BytesIO(data), cfg)
        pptx = None
        if not analysis["blocked"]:
            pptx = build_deck(analysis)
            assert_chart_cache(pptx, analysis)  # never ship unverified XML
        return analysis, pptx

    try:
        analysis, pptx = await run_upload_task(request, _analyze_and_build)
    except ValueError as e:  # V1 — wrong sheet shape
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _td(request)(str(e))},
            status_code=422)
    except VerificationError as e:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Internal cross-check failed — report not generated: {e}", e=e)},
            status_code=500)
    except Exception as e:  # corrupt zip, wrong format, … — never a 500
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file as .xlsx: {e}", e=e)},
            status_code=422)

    token = EFF_REPORTS.put({"analysis": analysis, "pptx": pptx,
                             "filename": filename, "remap": remap_note_dict})
    return RedirectResponse(f"/efficiency/{token}", status_code=303)


@router.post("/efficiency", response_class=HTMLResponse)
async def efficiency_run(request: Request, report: UploadFile,
                         basis: str = Form("pooled"),
                         tier_mode: str = Form("label"),
                         language: str = Form("en")):
    cfg_raw = {
        "basis": basis if basis in EFF_BASES else "pooled",
        "tier_mode": tier_mode if tier_mode in EFF_TIER_MODES else "label",
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
        )
    except UploadLimitError as e:
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": _tr(request)(str(e))},
            status_code=413)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "shared/error.html",
            {"message": _tr(request)(
                "Could not read the uploaded file as .xlsx: {e}", e=e)},
            status_code=422,
        )
    filename = Path(report.filename or "report.xlsx").stem

    # cheap pre-check: does the header fingerprint bind at all? If not, offer
    # the LLM mapping + human audit instead of a bare V1 error. The workbook
    # stays in memory throughout — client data is never written to disk.
    try:
        await run_upload_task(request, parse_eff_report, io.BytesIO(data))
    except ValueError as e:
        try:
            outcome = await run_upload_task(
                request, attempt_remap, "eff", data
            )
            if outcome.status == "cached":
                mapping = outcome.mapping
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
                return await _efficiency_analyze(
                    request, remapped, filename, cfg_raw,
                    remap_note(mapping, "eff", auto=True))
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
        except Exception as mapping_error:
            message = _td(request)(str(e))
            message += f" ({_tr(request)('Header mapping also failed: {e}', e=mapping_error)})"
            return templates.TemplateResponse(
                request, "shared/error.html", {"message": message},
                status_code=422)
    except Exception:
        pass  # not a header problem — let the real path produce the error

    return await _efficiency_analyze(request, data, filename, cfg_raw, None)


async def _apply_remap_eff(request: Request, token: str, entry: dict,
                           approved: dict, username: str):
    """Continuation of POST /remap/{token}/apply for the efficiency flow —
    everything stays in memory."""
    remap = dict(entry.get("remap") or {})
    data = entry["data"]
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
        mapper.cache_put(kind, m["sig"], m["sheet"], m["header_row"],
                         m["columns"], username)
        remap[kind] = remap_note(
            {**m, "approved_by": username}, kind, auto=False)
    return await _efficiency_analyze(
        request, data, entry["filename"], entry["cfg"], remap.get("eff"))


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
    return Response(
        entry["pptx"],
        media_type="application/vnd.openxmlformats-officedocument"
                   ".presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
