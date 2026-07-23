"""Environment-driven configuration.

All external credentials come from environment variables (Fly secrets in
production). Nothing here is required for parsing/matching to work — the
pipeline degrades gracefully when TikHub / Anthropic keys are absent.
"""
from __future__ import annotations

import math
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _positive_int_env(name: str, default: str) -> int:
    """Read a positive integer setting, failing fast on unsafe values."""
    value = int(_env(name, default))
    if value < 1:
        raise ValueError(f"{name} must be >= 1 (got {value})")
    return value


def _nonnegative_int_env(name: str, default: str) -> int:
    """Read an integer setting where zero is meaningful but negatives are not."""
    value = int(_env(name, default))
    if value < 0:
        raise ValueError(f"{name} must be >= 0 (got {value})")
    return value


def _positive_finite_float_env(name: str, default: str) -> float:
    """Read a finite, positive duration or timeout setting."""
    value = float(_env(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a finite number > 0 (got {value!r})"
        )
    return value


def _bounded_int_env(
        name: str, default: str, *, minimum: int, maximum: int) -> int:
    """Read an integer constrained to an operationally safe range."""
    value = int(_env(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum} (got {value})"
        )
    return value


def _bool_env(name: str, default: str = "0") -> bool:
    value = _env(name, default).casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (got {value!r})")


DATA_DIR = Path(_env("DATA_DIR", "/data" if Path("/data").is_dir() else "./data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "dmr_reconciler.sqlite3"

TIKHUB_API_KEY = _env("TIKHUB_API_KEY")
TIKHUB_BASE_URL = _env("TIKHUB_BASE_URL", "https://api.tikhub.io")
# Current TikHub XHS endpoints (verified against api.tikhub.io/openapi.json at
# build time — the /api/v1/xhs/... family no longer exists). Both accept either
# a note_id or a raw share link (share_text), so xhslink.com short links can be
# passed without following redirects ourselves.
TIKHUB_IMAGE_NOTE_PATH = _env(
    "TIKHUB_IMAGE_NOTE_PATH", "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
)
TIKHUB_VIDEO_NOTE_PATH = _env(
    "TIKHUB_VIDEO_NOTE_PATH", "/api/v1/xiaohongshu/app_v2/get_video_note_detail"
)
TIKHUB_TIMEOUT = _positive_finite_float_env("TIKHUB_TIMEOUT", "15")
TIKHUB_MAX_RETRIES = _nonnegative_int_env("TIKHUB_MAX_RETRIES", "3")
TIKHUB_CONCURRENCY = _bounded_int_env(
    "TIKHUB_CONCURRENCY", "8", minimum=1, maximum=32
)

DIRECT_HTTP_TIMEOUT = _positive_finite_float_env("DIRECT_HTTP_TIMEOUT", "3")
DIRECT_HTTP_RETRIES = _nonnegative_int_env("DIRECT_HTTP_RETRIES", "0")

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# The build prompt asked for "the latest Sonnet model string, verified at build
# time". Verified 2026-07: the current Sonnet is claude-sonnet-5 (the prompt's
# claude-sonnet-4-6 is the previous generation and still works as an override).
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_MAX_TOKENS = int(_env("ANTHROPIC_MAX_TOKENS", "2000"))

APP_PASSWORD = _env("APP_PASSWORD")
# Authentication is secure-by-default. Local open mode requires an explicit
# opt-out so a missing deployment secret cannot expose client workbooks.
ALLOW_OPEN_ACCESS = _bool_env("ALLOW_OPEN_ACCESS")
SESSION_COOKIE_SECURE = _bool_env("SESSION_COOKIE_SECURE", "1")
# Cookie-signing secret. Optional: when unset, a random secret is generated
# once and persisted under DATA_DIR (auth.service.signing_secret). It is
# deliberately NOT derived from APP_PASSWORD — the setup code is handed to
# every teammate and must not let its holders forge session cookies.
APP_SECRET = _env("APP_SECRET")

# Soft ranking window for Tier-3 candidate dates (days). Evidence: verified
# same-post matches at Δ=2 and Δ=4 days; a genuine different-post pair at Δ=2.
CANDIDATE_DATE_WINDOW_DAYS = _nonnegative_int_env(
    "CANDIDATE_DATE_WINDOW_DAYS", "7"
)

# At most this many reconciliation runs execute at once; excess run starts
# stay 'queued' and begin as slots free up (each run is CPU- and API-heavy).
RUN_MAX_CONCURRENT = _positive_int_env("RUN_MAX_CONCURRENT", "2")

# Compressed request, expanded XLSX, archive-entry, and on-disk retention
# budgets. These protect the 512 MB process and 1 GB Fly volume.
MAX_UPLOAD_MB = _positive_int_env("MAX_UPLOAD_MB", "25")
MAX_XLSX_UNCOMPRESSED_MB = _positive_int_env(
    "MAX_XLSX_UNCOMPRESSED_MB", "50"
)
MAX_XLSX_ENTRIES = _positive_int_env("MAX_XLSX_ENTRIES", "2000")
MAX_XLSX_SHEETS = _positive_int_env("MAX_XLSX_SHEETS", "64")
MAX_XLSX_ROW_INDEX = _positive_int_env("MAX_XLSX_ROW_INDEX", "150000")
MAX_XLSX_COLUMN_INDEX = _positive_int_env("MAX_XLSX_COLUMN_INDEX", "256")
# Normal-mode openpyxl materializes every populated cell. An expanded-byte
# limit stops ZIP bombs; this independent count also bounds object growth for
# highly repetitive, cell-dense worksheet XML.
MAX_XLSX_CELLS = _positive_int_env("MAX_XLSX_CELLS", "600000")
# Logical row limits are deliberately independent of XLSX cell count. A
# sparse two-column workbook can otherwise create hundreds of thousands of
# Python domain objects and a similarly large persisted result.
MAX_PLOG_ROWS = _positive_int_env("MAX_PLOG_ROWS", "50000")
MAX_DMR_ROWS = _positive_int_env("MAX_DMR_ROWS", "100000")
MAX_PERIMETER_ROWS = _positive_int_env("MAX_PERIMETER_ROWS", "100000")
# Read-only openpyxl iterates a rectangle, including absent cells. This
# independent budget prevents sparse dimensions from synthesizing millions
# of values while the perimeter is streamed.
MAX_PERIMETER_SCAN_CELLS = _positive_int_env(
    "MAX_PERIMETER_SCAN_CELLS", "5000000"
)
MAX_EFFICIENCY_ROWS = _positive_int_env("MAX_EFFICIENCY_ROWS", "50000")
MAX_CANDIDATES_PER_VERDICT = _positive_int_env(
    "MAX_CANDIDATES_PER_VERDICT", "25"
)
MAX_RESULT_MB = _positive_int_env("MAX_RESULT_MB", "64")
MAX_PERIMETER_CACHE_MB = _positive_int_env("MAX_PERIMETER_CACHE_MB", "64")
UPLOAD_REQUEST_CONCURRENCY = _positive_int_env(
    "UPLOAD_REQUEST_CONCURRENCY", "2"
)
UPLOAD_PARSE_CONCURRENCY = _positive_int_env("UPLOAD_PARSE_CONCURRENCY", "1")
EXPORT_STREAM_CONCURRENCY = _positive_int_env(
    "EXPORT_STREAM_CONCURRENCY", "4"
)
UPLOAD_RETENTION_HOURS = _positive_int_env("UPLOAD_RETENTION_HOURS", "720")
UPLOAD_MAX_TOTAL_MB = _positive_int_env("UPLOAD_MAX_TOTAL_MB", "500")
DB_MAX_TOTAL_MB = _positive_int_env("DB_MAX_TOTAL_MB", "400")
DATA_MAX_TOTAL_MB = _positive_int_env("DATA_MAX_TOTAL_MB", "900")
LINK_CACHE_MAX_ROWS = _positive_int_env("LINK_CACHE_MAX_ROWS", "100000")
LINK_CACHE_MAX_RAW_MB = _positive_int_env("LINK_CACHE_MAX_RAW_MB", "128")
PERIMETER_CACHE_MAX_ROWS = _positive_int_env(
    "PERIMETER_CACHE_MAX_ROWS", "100"
)
MAINTENANCE_INTERVAL_SECONDS = _positive_int_env(
    "MAINTENANCE_INTERVAL_SECONDS", "60"
)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = MAX_XLSX_UNCOMPRESSED_MB * 1024 * 1024
UPLOAD_MAX_TOTAL_BYTES = UPLOAD_MAX_TOTAL_MB * 1024 * 1024
DB_MAX_TOTAL_BYTES = DB_MAX_TOTAL_MB * 1024 * 1024
DATA_MAX_TOTAL_BYTES = DATA_MAX_TOTAL_MB * 1024 * 1024
MAX_RESULT_BYTES = MAX_RESULT_MB * 1024 * 1024
MAX_PERIMETER_CACHE_BYTES = MAX_PERIMETER_CACHE_MB * 1024 * 1024
LINK_CACHE_MAX_RAW_BYTES = LINK_CACHE_MAX_RAW_MB * 1024 * 1024

# Failed link resolutions are cached; retry them only after this many hours
# (or when a run explicitly requests it). Successes are cached permanently.
FAILED_CACHE_TTL_HOURS = _positive_int_env("FAILED_CACHE_TTL_HOURS", "24")


def validate_runtime() -> None:
    if not APP_PASSWORD and not ALLOW_OPEN_ACCESS:
        raise RuntimeError(
            "APP_PASSWORD is required. For intentional local open mode, "
            "set ALLOW_OPEN_ACCESS=1."
        )
    if UPLOAD_MAX_TOTAL_BYTES + DB_MAX_TOTAL_BYTES > DATA_MAX_TOTAL_BYTES:
        raise RuntimeError(
            "UPLOAD_MAX_TOTAL_MB + DB_MAX_TOTAL_MB must not exceed "
            "DATA_MAX_TOTAL_MB."
        )


def ensure_dirs() -> None:
    if DATA_DIR.resolve(strict=False) == Path("/"):
        raise RuntimeError("DATA_DIR must not be the filesystem root")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
