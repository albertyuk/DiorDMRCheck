"""Human-audit routes for LLM header mappings.

The audit screen serves BOTH products; each product registers the
continuation for its flow in FLOW_HANDLERS at import time (reconciler:
"run", efficiency: "eff"), so this module never encodes either pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..web import current_user, templates, tr as _tr
from .registry import FIELDS
from .service import PENDING_MAPS, audit_context, cleanup_pending_entry

router = APIRouter()

# flow name → async continuation(request, token, entry, approved, username).
# The continuation applies the approved mappings and resumes its product's
# upload flow; it owns popping the token on success.
FLOW_HANDLERS: dict[str, Callable[..., Awaitable]] = {}

# flow name → where "reject" returns the user to.
FLOW_HOMES = {"run": "/", "eff": "/efficiency"}


@router.get("/remap/{token}", response_class=HTMLResponse)
async def remap_audit(request: Request, token: str):
    entry = PENDING_MAPS.get(token)
    if not entry:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "This mapping session has expired — upload the file again.")},
            status_code=404)
    return templates.TemplateResponse(request, "shared/remap_audit.html",
                                      audit_context(token, entry))


@router.post("/remap/{token}/reject")
async def remap_reject(request: Request, token: str):
    claim_status, entry = PENDING_MAPS.claim(token)
    if claim_status == "busy":
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("Working…")}, status_code=409)
    if claim_status == "claimed":
        PENDING_MAPS.pop(token)
        assert entry is not None
        cleanup_pending_entry(entry)
    dest = FLOW_HOMES.get(entry["flow"] if entry else "run", "/")
    return RedirectResponse(dest, status_code=303)


@router.post("/remap/{token}/apply", response_class=HTMLResponse)
async def remap_apply(request: Request, token: str):
    entry = PENDING_MAPS.get(token)
    if not entry:
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "This mapping session has expired — upload the file again.")},
            status_code=404)
    form = await request.form()
    tr = _tr(request)
    user = current_user(request)

    # collect + validate the (possibly human-corrected) selections
    approved: dict[str, dict] = {}
    for kind, audit in entry["audits"].items():
        columns: dict[str, int] = {}
        for text, key, required, _desc in FIELDS[kind]:
            raw = str(form.get(f"{kind}:{key}", "")).strip()
            if raw:
                columns[key] = int(raw)
            elif required:
                return templates.TemplateResponse(
                    request, "shared/remap_audit.html",
                    audit_context(token, entry, error=tr(
                        "Required field {field} has no column selected.",
                        field=text)),
                    status_code=422)
        if len(set(columns.values())) != len(columns):
            return templates.TemplateResponse(
                request, "shared/remap_audit.html",
                audit_context(token, entry, error=tr(
                    "Two fields point at the same column — each column can "
                    "serve only one field.")),
                status_code=422)
        approved[kind] = {
            "sheet": audit["proposal"]["sheet"],
            "header_row": int(audit["proposal"]["header_row"]),
            "columns": columns, "sig": audit["sig"],
        }

    username = (user or {}).get("username", "") or "open-mode"
    claim_status, claimed_entry = PENDING_MAPS.claim(token)
    if claim_status == "missing":
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": tr(
                "This mapping session has expired — upload the file again.")},
            status_code=404)
    if claim_status == "busy":
        return templates.TemplateResponse(
            request, "shared/error.html", {"message": tr("Working…")},
            status_code=409)

    # Claiming happens only after the editable form has passed validation,
    # so a 422 leaves the token available for correction. A continuation
    # failure releases it for retry; any normal response consumes it exactly
    # once (legacy continuations may also pop it, which remains harmless).
    assert claimed_entry is not None
    try:
        handler = FLOW_HANDLERS[claimed_entry["flow"]]
        response = await handler(
            request, token, claimed_entry, approved, username)
    except BaseException:
        run_dir = claimed_entry.get("run_dir")
        if (claimed_entry.get("flow") == "run" and run_dir
                and not Path(run_dir).exists()):
            # The real continuation cleans staging before propagating fatal
            # DB/cancellation failures. Such a token can no longer be retried.
            PENDING_MAPS.pop(token)
        else:
            PENDING_MAPS.release(token)
        raise
    PENDING_MAPS.pop(token)
    return response
