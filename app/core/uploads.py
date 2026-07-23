"""Bounded workbook upload and staging-file lifecycle helpers."""
from __future__ import annotations

import asyncio
import io
import posixpath
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Callable, Union
from urllib.parse import unquote
from defusedxml import ElementTree

from fastapi import Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse

from .. import config

CHUNK_BYTES = 1024 * 1024

_lifecycle_lock = threading.RLock()
_active_uploads: dict[str, int] = {}
_cleanup_claims: set[str] = set()
_workbook_gate = threading.BoundedSemaphore(config.UPLOAD_PARSE_CONCURRENCY)


class UploadLimitError(ValueError):
    """An uploaded workbook exceeded a configured resource limit."""


class _RequestBodyTooLarge(OSError):
    """OSError makes Starlette close partially spooled multipart files."""

    pass


def register_active_upload(path: Path) -> bool:
    """Protect a staging directory until it has durable lifecycle metadata."""
    with _lifecycle_lock:
        if path.name in _cleanup_claims:
            return False
        _active_uploads[path.name] = _active_uploads.get(path.name, 0) + 1
        return True


def unregister_active_upload(path: Path) -> None:
    with _lifecycle_lock:
        remaining = _active_uploads.get(path.name, 0) - 1
        if remaining > 0:
            _active_uploads[path.name] = remaining
        else:
            _active_uploads.pop(path.name, None)


def active_upload_names() -> set[str]:
    with _lifecycle_lock:
        return set(_active_uploads)


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before Starlette's multipart spooling.

    The receive wrapper also enforces the limit when Content-Length is absent
    or dishonest, so chunked requests cannot bypass the ingress budget.
    """

    def __init__(self, app, limit_for_path: Callable[[str, str], int],
                 admission_for_path: Callable[[str, str], bool] | None = None,
                 max_concurrent_uploads: int = 1):
        self.app = app
        self.limit_for_path = limit_for_path
        self.admission_for_path = admission_for_path
        self.upload_admission = asyncio.Semaphore(max_concurrent_uploads)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if (self.admission_for_path is not None
                and self.admission_for_path(method, path)):
            # Admission happens before FastAPI parses/spools multipart data,
            # bounding aggregate in-memory and on-disk uploads as well as
            # individual request size.
            async with self.upload_admission:
                await self._call_http(scope, receive, send, method, path)
            return
        await self._call_http(scope, receive, send, method, path)

    async def _call_http(self, scope, receive, send, method: str, path: str):
        limit = self.limit_for_path(
            method, path
        )
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    await PlainTextResponse(
                        "Request body too large.", status_code=413
                    )(scope, receive, send)
                    return
            except ValueError:
                pass

        received = 0
        response_started = False
        overflowed = False
        overflow_response_sent = False

        async def limited_receive():
            nonlocal received, overflowed
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflowed = True
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started, overflow_response_sent
            if overflowed:
                # FastAPI converts receive errors during multipart parsing to
                # a generic 400. Replace that response with the actual limit
                # result while suppressing the downstream error body.
                if not overflow_response_sent:
                    overflow_response_sent = True
                    await PlainTextResponse(
                        "Request body too large.", status_code=413
                    )(scope, receive, send)
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if overflow_response_sent:
                return
            if response_started:
                raise
            await PlainTextResponse(
                "Request body too large.", status_code=413
            )(scope, receive, send)


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` without one unbounded ``read()`` call."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise UploadLimitError(
                f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        chunks.append(chunk)


async def save_limited(upload: UploadFile, path: Path, max_bytes: int) -> int:
    """Stream an upload to ``path`` and remove partial output on failure."""
    total = 0
    try:
        with path.open("wb") as output:
            while True:
                chunk = await upload.read(
                    min(CHUNK_BYTES, max_bytes - total + 1)
                )
                if not chunk:
                    return total
                total += len(chunk)
                if total > max_bytes:
                    raise UploadLimitError(
                        f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit."
                    )
                output.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def run_upload_task(request: Request, func: Callable, *args: Any,
                          **kwargs: Any):
    """Run workbook-heavy work under the process-wide memory gate."""
    del request  # kept in the API so route call sites remain self-documenting
    cancelled = threading.Event()
    worker = asyncio.create_task(
        run_in_threadpool(_run_gated, func, args, kwargs, cancelled)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # A client disconnect must not detach a worker that still owns upload
        # bytes or staging paths. Retain request ownership until it finishes,
        # then propagate cancellation so normal cleanup can run safely.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()  # retrieve a possible error; cancellation wins
        raise


def _run_gated(func: Callable, args: tuple, kwargs: dict,
               cancelled: threading.Event | None = None):
    # Acquire, optional work, and release happen in one worker invocation, so
    # cancellation cannot strand a permit. Work abandoned while still queued
    # is skipped as soon as its turn reaches the gate.
    with _workbook_gate:
        if cancelled is not None and cancelled.is_set():
            return None
        return func(*args, **kwargs)


def run_upload_task_sync(func: Callable, *args: Any, **kwargs: Any):
    """Synchronous counterpart used by background reconciliation threads."""
    return _run_gated(func, args, kwargs, None)


ArchiveSource = Union[bytes, bytearray, Path, str, BinaryIO]
_CELL_REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return index


def _validate_coordinate(reference: str, *, max_row_index: int | None,
                         max_column_index: int | None) -> None:
    """Reject worksheet dimensions/cells that force huge sparse iteration."""
    match = _CELL_REF_RE.fullmatch(reference or "")
    if match is None:
        return
    column, row_text = match.groups()
    row = int(row_text)
    if max_row_index is not None and row > max_row_index:
        raise UploadLimitError(
            "Workbook uses a worksheet row beyond the configured "
            f"{max_row_index:,} row-index limit."
        )
    column_number = _column_index(column)
    if max_column_index is not None and column_number > max_column_index:
        raise UploadLimitError(
            "Workbook uses a worksheet column beyond the configured "
            f"{max_column_index:,} column-index limit."
        )


def _normalize_part_path(name: str) -> str | None:
    part = posixpath.normpath(name.replace("\\", "/").lstrip("/"))
    if part in {"", ".", ".."} or part.startswith("../"):
        return None
    return part


def _canonical_part_name(name: str) -> str | None:
    """Decode and normalize one OPC part URI exactly once."""
    return _normalize_part_path(unquote(name))


def _relationship_owner(rels_name: str) -> str | None:
    """Return the package part owning an OPC relationships member."""
    if rels_name.startswith("_rels/"):
        return rels_name.removeprefix("_rels/").removesuffix(".rels")
    marker = "/_rels/"
    if marker not in rels_name:
        return None
    prefix, filename = rels_name.split(marker, 1)
    return posixpath.join(prefix, filename.removesuffix(".rels"))


def _resolve_part(owner: str, target: str) -> str | None:
    canonical_owner = _canonical_part_name(owner)
    if canonical_owner is None:
        return None
    target = unquote(target).replace("\\", "/")
    if target.startswith("/"):
        part = target.lstrip("/")
    else:
        part = posixpath.join(
            posixpath.dirname(canonical_owner), target
        )
    return _normalize_part_path(part)


def _worksheet_parts(archive: zipfile.ZipFile,
                     infos: list[zipfile.ZipInfo]) -> set[str]:
    """Discover actual worksheet parts independent of name/extension."""
    parts: set[str] = set()
    for info in infos:
        name = info.filename.lstrip("/")
        if name == "[Content_Types].xml":
            with archive.open(info) as stream:
                for _event, element in ElementTree.iterparse(
                        stream, events=("end",), forbid_dtd=True,
                        forbid_entities=True, forbid_external=True):
                    if (_local_name(element.tag) == "Override"
                            and element.attrib.get("ContentType", "").lower()
                            .endswith("worksheet+xml")):
                        part = _canonical_part_name(
                            element.attrib.get("PartName", "")
                        )
                        if part:
                            parts.add(part)
                    element.clear()
        elif name.lower().endswith(".rels"):
            owner = _relationship_owner(name)
            if owner is None:
                continue
            with archive.open(info) as stream:
                for _event, element in ElementTree.iterparse(
                        stream, events=("end",), forbid_dtd=True,
                        forbid_entities=True, forbid_external=True):
                    if (_local_name(element.tag) == "Relationship"
                            and element.attrib.get("Type", "").lower()
                            .endswith("/worksheet")
                            and element.attrib.get("TargetMode", "").lower()
                            != "external"):
                        part = _resolve_part(
                            owner, element.attrib.get("Target", "")
                        )
                        if part:
                            parts.add(part)
                    element.clear()
    return parts


def validate_xlsx_archive(source: ArchiveSource, *, max_uncompressed_bytes: int,
                          max_entries: int,
                          max_cells: int | None = None,
                          max_sheets: int | None = None,
                          max_row_index: int | None = None,
                          max_column_index: int | None = None) -> None:
    """Reject archives that could expand beyond the process safety budget.

    The HTTP/compressed size is bounded separately.  XLSX is a ZIP container,
    so checking its central directory before openpyxl touches XML prevents a
    small compressed upload from expanding to hundreds of megabytes.
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise UploadLimitError(
                    f"Workbook contains more than {max_entries} files."
                )
            total = sum(info.file_size for info in infos)
            if total > max_uncompressed_bytes:
                limit_mb = max_uncompressed_bytes // (1024 * 1024)
                raise UploadLimitError(
                    f"Workbook expands beyond the {limit_mb} MB safety limit."
                )
            if any(limit is not None for limit in (
                    max_cells, max_sheets, max_row_index,
                    max_column_index)):
                cell_count = 0
                worksheet_parts = _worksheet_parts(archive, infos)
                if (max_sheets is not None
                        and len(worksheet_parts) > max_sheets):
                    raise UploadLimitError(
                        "Workbook contains more than "
                        f"{max_sheets:,} worksheets."
                    )
                for worksheet in infos:
                    if (_canonical_part_name(worksheet.filename)
                            not in worksheet_parts):
                        continue
                    with archive.open(worksheet) as xml:
                        for _event, element in ElementTree.iterparse(
                                xml, events=("end",), forbid_dtd=True,
                                forbid_entities=True, forbid_external=True):
                            local_name = _local_name(element.tag)
                            if local_name == "dimension":
                                for coordinate in element.attrib.get(
                                        "ref", "").split(":"):
                                    _validate_coordinate(
                                        coordinate,
                                        max_row_index=max_row_index,
                                        max_column_index=max_column_index,
                                    )
                            elif local_name == "row":
                                row = element.attrib.get("r", "")
                                if row.isdigit() and max_row_index is not None:
                                    _validate_coordinate(
                                        "A" + row,
                                        max_row_index=max_row_index,
                                        max_column_index=max_column_index,
                                    )
                            elif local_name == "c":
                                _validate_coordinate(
                                    element.attrib.get("r", ""),
                                    max_row_index=max_row_index,
                                    max_column_index=max_column_index,
                                )
                                cell_count += 1
                                if (max_cells is not None
                                        and cell_count > max_cells):
                                    raise UploadLimitError(
                                        "Workbook contains more than "
                                        f"{max_cells:,} populated cells."
                                    )
                            element.clear()
    except zipfile.BadZipFile:
        # Preserve the existing parser-facing corrupt-workbook diagnostics.
        return


def remove_tree(path: Path) -> None:
    """Best-effort removal for a run's private staging directory."""
    shutil.rmtree(path, ignore_errors=True)


def cleanup_expired(
    root: Path,
    max_age_seconds: float,
    *,
    now: float | None = None,
    should_remove: Callable[[Path], bool] | None = None,
    on_remove: Callable[[Path], None] | None = None,
) -> int:
    """Remove old directories and reconcile their external metadata."""
    if not root.exists():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    candidates: list[Path] = []
    try:
        for path in root.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    candidates.append(path)
            except OSError:
                continue
    except OSError:
        return 0

    removed = 0
    for path in candidates:
        trash = _claim_cleanup(path, should_remove)
        if trash is None:
            continue
        if not _complete_cleanup_claim(path, trash, on_remove):
            continue
        remove_tree(trash)
        if not trash.exists():
            removed += 1
    return removed


def cleanup_over_budget(
    root: Path,
    max_total_bytes: int,
    *,
    should_remove: Callable[[Path], bool] | None = None,
    on_remove: Callable[[Path], None] | None = None,
) -> int:
    """Remove oldest eligible directories until the aggregate fits."""
    if not root.exists():
        return 0
    directories: list[tuple[Path, int, float]] = []
    try:
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                size = sum(file.stat().st_size for file in path.rglob("*")
                           if file.is_file())
                directories.append((path, size, path.stat().st_mtime))
            except OSError:
                continue
    except OSError:
        return 0
    total = sum(size for _path, size, _mtime in directories)
    removed = 0
    for path, size, _mtime in sorted(directories, key=lambda item: item[2]):
        if total <= max_total_bytes:
            break
        trash = _claim_cleanup(path, should_remove)
        if trash is None:
            continue
        if not _complete_cleanup_claim(path, trash, on_remove):
            continue
        remove_tree(trash)
        if not trash.exists():
            total -= size
            removed += 1
    return removed


def _claim_cleanup(
    path: Path,
    should_remove: Callable[[Path], bool] | None,
) -> Path | None:
    """Atomically detach an eligible directory, then let deletion run free.

    Only the eligibility recheck, same-filesystem rename, and metadata update
    hold the lifecycle lock. Recursive scans/deletes stay outside it, so an
    async request never blocks its event loop behind volume-sized I/O.
    """
    try:
        # Potential DB/token-store checks stay outside the lifecycle lock.
        if should_remove is not None and not should_remove(path):
            return None
    except Exception:
        return None
    with _lifecycle_lock:
        try:
            if (not path.is_dir() or path.name in _active_uploads
                    or path.name in _cleanup_claims):
                return None
            trash = path.with_name(
                f".cleanup-{path.name}-{uuid.uuid4().hex}"
            )
            path.rename(trash)
            _cleanup_claims.add(path.name)
            return trash
        except OSError:
            return None


def _complete_cleanup_claim(
    path: Path,
    trash: Path,
    on_remove: Callable[[Path], None] | None,
) -> bool:
    """Reconcile metadata outside the lock, restoring files on failure."""
    callback_ok = True
    if on_remove is not None:
        try:
            on_remove(path)
        except Exception:
            callback_ok = False
    with _lifecycle_lock:
        if not callback_ok:
            try:
                if not path.exists() and trash.exists():
                    trash.rename(path)
            except OSError:
                pass
        _cleanup_claims.discard(path.name)
    return callback_ok
