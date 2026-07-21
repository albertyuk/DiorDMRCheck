"""DMR Reconciler — ASGI app assembly.

Wires the shared middleware/static/startup concerns and mounts one router
per feature: auth/team, the reconciliation flow (upload → preview → run →
results → exports), the shared header-remap audit, and the KOL efficiency
report. All handler logic lives in those routers; this module only
assembles.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, i18n
from .auth.routes import auth_middleware, router as auth_router
from .core import db
from .core.uploads import (RequestBodyLimitMiddleware, active_upload_names,
                           cleanup_expired, cleanup_over_budget)
from .efficiency.routes import EFF_REPORTS, router as efficiency_router
from .reconciler import runs
from .reconciler.routes import router as reconciler_router
from .remap.routes import router as remap_router
from .remap.service import PENDING_MAPS

_FORM_BODY_LIMIT = 1024 * 1024
_MULTIPART_OVERHEAD = 64 * 1024


def _request_body_limit(method: str, path: str) -> int:
    if method not in {"POST", "PUT", "PATCH"}:
        return 0
    if path == "/upload":
        return 3 * config.MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD
    if path == "/efficiency":
        return config.MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD
    return _FORM_BODY_LIMIT


def _is_upload_request(method: str, path: str) -> bool:
    return method == "POST" and path in {"/upload", "/efficiency"}


def _cleanup_uploads() -> None:
    def removable(path: Path) -> bool:
        pending = {
            Path(entry["run_dir"]).name
            for entry in PENDING_MAPS.entries()
            if entry.get("flow") == "run" and entry.get("run_dir")
        }
        if path.name in active_upload_names() or path.name in pending:
            return False
        run = db.run_get(path.name)
        return not run or run.get("status") not in {"queued", "running"}

    def forget_run(path: Path) -> None:
        db.run_delete(path.name)

    kwargs = {"should_remove": removable, "on_remove": forget_run}
    cleanup_expired(
        config.UPLOAD_DIR,
        config.UPLOAD_RETENTION_HOURS * 3600,
        **kwargs,
    )
    cleanup_over_budget(
        config.UPLOAD_DIR,
        config.UPLOAD_MAX_TOTAL_BYTES,
        **kwargs,
    )


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(config.MAINTENANCE_INTERVAL_SECONDS)
        PENDING_MAPS.discard_expired()
        EFF_REPORTS.discard_expired()
        await asyncio.to_thread(_cleanup_uploads)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.validate_runtime()
    config.ensure_dirs()
    runs.recover_orphans()
    PENDING_MAPS.discard_expired()
    EFF_REPORTS.discard_expired()
    await asyncio.to_thread(_cleanup_uploads)
    maintenance = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance


app = FastAPI(title="DMR Reconciler", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")
app.middleware("http")(auth_middleware)
app.add_middleware(RequestBodyLimitMiddleware,
                   limit_for_path=_request_body_limit,
                   admission_for_path=_is_upload_request,
                   max_concurrent_uploads=config.UPLOAD_REQUEST_CONCURRENCY)


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
    ref = urlparse(request.headers.get("referer", ""))
    back = ref.path or "/"
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    elif ref.query:  # keep e.g. /team?msg=… flash messages across the toggle
        back += "?" + ref.query
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie(i18n.COOKIE, code, max_age=365 * 24 * 3600,
                    samesite="lax", secure=config.SESSION_COOKIE_SECURE)
    return resp


app.include_router(auth_router)
app.include_router(remap_router)
app.include_router(reconciler_router)
app.include_router(efficiency_router)
