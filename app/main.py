"""DMR Reconciler — ASGI app assembly.

Wires the shared middleware/static/startup concerns and mounts one router
per feature: auth/team, the reconciliation flow (upload → preview → run →
results → exports), the shared header-remap audit, and the KOL efficiency
report. All handler logic lives in those routers; this module only
assembles.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import i18n
from .auth.routes import auth_middleware, router as auth_router
from .efficiency.routes import router as efficiency_router
from .reconciler import runs
from .reconciler.routes import router as reconciler_router
from .remap.routes import router as remap_router

app = FastAPI(title="DMR Reconciler")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")
app.middleware("http")(auth_middleware)


@app.on_event("startup")
async def recover_orphaned_runs() -> None:
    runs.recover_orphans()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


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


app.include_router(auth_router)
app.include_router(remap_router)
app.include_router(reconciler_router)
app.include_router(efficiency_router)
