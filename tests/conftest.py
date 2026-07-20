from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep test SQLite state away from any real /data volume.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", str(ROOT / ".test_data"))

from tests import fixtures  # noqa: E402


@pytest.fixture(scope="session")
def plog_path(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("xlsx") / "PLOG_DMR_CHECK.xlsx"
    fixtures.build_plog(str(p))
    return str(p)


@pytest.fixture(scope="session")
def dmr_path(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("xlsx") / "YTD_DMR_MICRO.xlsx"
    fixtures.build_dmr(str(p))
    return str(p)


@pytest.fixture
def fake_resolver(monkeypatch):
    """Replace network resolution with the fixture table. Tests never touch
    the network or the SQLite cache."""
    from app.resolver import Resolution

    table = fixtures.fake_resolutions()

    def fake_resolve_link(url, run_counter=None, retry_failed=False):
        entry = table.get(url)
        if entry is None:
            return Resolution(status="failed", error="unknown fixture url")
        if "fail" in entry:
            return Resolution(status="failed", error=entry["fail"])
        return Resolution(
            status="ok", note_id=entry["note_id"], author_id=entry["author"],
            author_name=entry["nick"], likes=entry.get("likes"),
            source="fixture",
        )

    def fake_ensure_author(url, res, run_counter=None, retry_failed=False):
        return res

    import app.matcher as matcher_mod
    monkeypatch.setattr(matcher_mod, "resolve_link", fake_resolve_link)
    monkeypatch.setattr(matcher_mod, "ensure_author", fake_ensure_author)
    return table
