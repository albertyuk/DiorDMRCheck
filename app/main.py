"""DMR Reconciler — ASGI app assembly.

Wires the shared middleware/static/startup concerns and mounts one router
per feature: auth/team, the reconciliation flow (upload → preview → run →
results → exports), the shared header-remap audit, and the KOL efficiency
report. All handler logic lives in those routers; this module only
assembles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders
from fastapi.staticfiles import StaticFiles

from . import config, i18n
from .auth.routes import auth_middleware, router as auth_router
from .core import db
from .core.uploads import (RequestBodyLimitMiddleware, active_upload_names,
                           cleanup_expired, cleanup_over_budget)
from .efficiency.routes import EFF_REPORTS, router as efficiency_router
from .reconciler import links, runs
from .reconciler.routes import router as reconciler_router
from .remap.routes import router as remap_router
from .remap.service import PENDING_MAPS

_FORM_BODY_LIMIT = 1024 * 1024
_MULTIPART_OVERHEAD = 64 * 1024
logger = logging.getLogger(__name__)
_maintenance_task: asyncio.Task | None = None
_maintenance_state = {
    "last_success": 0.0,
    "consecutive_failures": 0,
    "last_error": "",
    "data_bytes": 0,
    "db_logical_bytes": 0,
}


class SecurityHeadersMiddleware:
    """Attach browser security policy to every normal HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault(
                    "Content-Security-Policy", "frame-ancestors 'self'"
                )
                headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


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


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _cleanup_uploads() -> dict[str, int]:
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
    # SQLite/WAL/cache and uploads share one volume. Reserve the bytes already
    # occupied by SQLite rather than pretending the upload subdirectory owns
    # the entire disk budget.
    available_upload_bytes = max(
        0, config.DATA_MAX_TOTAL_BYTES - db.database_storage_bytes()
    )
    cleanup_over_budget(
        config.UPLOAD_DIR,
        min(config.UPLOAD_MAX_TOTAL_BYTES, available_upload_bytes),
        **kwargs,
    )
    maintenance = db.database_maintenance(
        allow_full_vacuum=(not active_upload_names()
                           and db.active_run_count() == 0)
    )
    maintenance.update({
        "data_bytes": _tree_bytes(config.DATA_DIR),
        "db_logical_bytes": db.database_logical_bytes(),
    })
    return maintenance


def _maintenance_once() -> None:
    PENDING_MAPS.discard_expired()
    EFF_REPORTS.discard_expired()
    stats = _cleanup_uploads()
    _maintenance_state.update(stats)
    _maintenance_state.update({
        "last_success": time.monotonic(),
        "consecutive_failures": 0,
        "last_error": "",
    })


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(config.MAINTENANCE_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_maintenance_once)
        except Exception as exc:
            _maintenance_state["consecutive_failures"] += 1
            _maintenance_state["last_error"] = type(exc).__name__
            logger.exception("maintenance cycle failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _maintenance_task
    config.validate_runtime()
    config.ensure_dirs()
    runs.recover_orphans()
    await asyncio.to_thread(_maintenance_once)
    maintenance = asyncio.create_task(
        _maintenance_loop(), name="retention-maintenance"
    )
    _maintenance_task = maintenance
    try:
        yield
    finally:
        try:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
        finally:
            _maintenance_task = None
            await asyncio.to_thread(links.close_tikhub_client)


app = FastAPI(title="DMR Reconciler", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")
app.middleware("http")(auth_middleware)
app.add_middleware(RequestBodyLimitMiddleware,
                   limit_for_path=_request_body_limit,
                   admission_for_path=_is_upload_request,
                   max_concurrent_uploads=config.UPLOAD_REQUEST_CONCURRENCY)
app.add_middleware(SecurityHeadersMiddleware)


@app.get("/healthz")
async def healthz():
    age = time.monotonic() - _maintenance_state["last_success"]
    stale_after = max(180, config.MAINTENANCE_INTERVAL_SECONDS * 3)
    task_alive = _maintenance_task is not None and not _maintenance_task.done()
    storage_ok = (
        _maintenance_state["data_bytes"] <= config.DATA_MAX_TOTAL_BYTES
        and _maintenance_state["db_logical_bytes"] <= config.DB_MAX_TOTAL_BYTES
    )
    ok = bool(task_alive and age <= stale_after and storage_ok
              and db.healthcheck())
    return JSONResponse(
        {"ok": ok, "maintenance": "ok" if ok else "degraded"},
        status_code=200 if ok else 503,
    )


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
