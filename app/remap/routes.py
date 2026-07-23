"""Human-audit routes for LLM header mappings.

The audit screen serves BOTH products; each product registers the
continuation for its flow in FLOW_HANDLERS at import time (reconciler:
"run", efficiency: "eff"), so this module never encodes either pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
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


@router.post("/remap/cache/{kind}/{sig}/delete")
async def remap_cache_delete(request: Request, kind: str, sig: str,
                             next_path: str = Form("/")):
    """Let an administrator revoke a bad auto-applied global mapping."""
    user = current_user(request)
    if config.APP_PASSWORD and not (user and user.get("is_admin")):
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)(
                "Only administrators can revoke shared header mappings.")},
            status_code=403)
    if kind not in FIELDS or len(sig) != 32 or any(
            char not in "0123456789abcdef" for char in sig.casefold()):
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": _tr(request)("Header mapping not found.")},
            status_code=404)
    from . import mapper
    mapper.cache_delete(kind, sig.casefold())
    destination = next_path if next_path in FLOW_HOMES.values() else "/"
    return RedirectResponse(destination, status_code=303)


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
        if entry is None:
            raise RuntimeError("claimed mapping token has no payload")
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
    if config.APP_PASSWORD and not (user and user.get("is_admin")):
        return templates.TemplateResponse(
            request, "shared/error.html",
            {"message": tr(
                "Only administrators can approve shared header mappings.")},
            status_code=403)

    # collect + validate the (possibly human-corrected) selections
    approved: dict[str, dict] = {}
    for kind, audit in entry["audits"].items():
        columns: dict[str, int] = {}
        offered = {int(choice["col"]) for choice in audit["choices"]}
        for text, key, required, _desc in FIELDS[kind]:
            raw = str(form.get(f"{kind}:{key}", "")).strip()
            if raw:
                try:
                    selected = int(raw)
                except ValueError:
                    return templates.TemplateResponse(
                        request, "shared/remap_audit.html",
                        audit_context(token, entry, error=tr(
                            "Invalid column selected for {field}.",
                            field=text)), status_code=422)
                if selected not in offered:
                    return templates.TemplateResponse(
                        request, "shared/remap_audit.html",
                        audit_context(token, entry, error=tr(
                            "Invalid column selected for {field}.",
                            field=text)), status_code=422)
                columns[key] = selected
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
    if claimed_entry is None:
        raise RuntimeError("claimed mapping token has no payload")
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
