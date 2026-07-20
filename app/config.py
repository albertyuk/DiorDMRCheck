"""Environment-driven configuration.

All external credentials come from environment variables (Fly secrets in
production). Nothing here is required for parsing/matching to work — the
pipeline degrades gracefully when TikHub / Anthropic keys are absent.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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
TIKHUB_TIMEOUT = float(_env("TIKHUB_TIMEOUT", "15"))
TIKHUB_MAX_RETRIES = int(_env("TIKHUB_MAX_RETRIES", "3"))
TIKHUB_CONCURRENCY = int(_env("TIKHUB_CONCURRENCY", "4"))

DIRECT_HTTP_TIMEOUT = float(_env("DIRECT_HTTP_TIMEOUT", "5"))
DIRECT_HTTP_RETRIES = int(_env("DIRECT_HTTP_RETRIES", "2"))

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# The build prompt asked for "the latest Sonnet model string, verified at build
# time". Verified 2026-07: the current Sonnet is claude-sonnet-5 (the prompt's
# claude-sonnet-4-6 is the previous generation and still works as an override).
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_MAX_TOKENS = int(_env("ANTHROPIC_MAX_TOKENS", "2000"))

APP_PASSWORD = _env("APP_PASSWORD")
# Cookie-signing secret. Falls back to a derivation of APP_PASSWORD so a
# single secret is enough to configure auth.
APP_SECRET = _env("APP_SECRET") or ("dmr-" + APP_PASSWORD)

# Soft ranking window for Tier-3 candidate dates (days). Evidence: verified
# same-post matches at Δ=2 and Δ=4 days; a genuine different-post pair at Δ=2.
CANDIDATE_DATE_WINDOW_DAYS = int(_env("CANDIDATE_DATE_WINDOW_DAYS", "7"))

# Failed link resolutions are cached; retry them only after this many hours
# (or when a run explicitly requests it). Successes are cached permanently.
FAILED_CACHE_TTL_HOURS = int(_env("FAILED_CACHE_TTL_HOURS", "24"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
