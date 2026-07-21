"""Human-audit routes for LLM header mappings.

The audit screen serves BOTH products; each product registers the
continuation for its flow in FLOW_HANDLERS at import time (reconciler:
"run", efficiency: "eff"), so this module never encodes either pipeline.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..web import current_user, templates, tr as _tr
from .registry import FIELDS
from .service import PENDING_MAPS, audit_context

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
async def remap_reject(token: str):
    entry = PENDING_MAPS.pop(token)
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
    handler = FLOW_HANDLERS[entry["flow"]]
    return await handler(request, token, entry, approved, username)
