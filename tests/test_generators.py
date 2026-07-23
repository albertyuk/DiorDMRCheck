"""Repository deliverable generators remain runnable after package refactors."""
from __future__ import annotations

import io
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.efficiency.analysis import ReportConfig, analyze
from app.reconciler.parsers import parse_dmr, parse_plog

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def test_demo_generator_imports_current_modules_and_builds_valid_data():
    namespace = runpy.run_path(str(TOOLS / "make_demo_assets.py"))
    data = namespace["build_demo_bytes"]()
    report = analyze(io.BytesIO(data), ReportConfig())
    assert not report["blocked"]
    assert report["metrics"]["totals"]["rows"] > 0


def test_template_generator_writes_parseable_docs(tmp_path):
    subprocess.run(
        [sys.executable, str(TOOLS / "make_templates.py"), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    plog = tmp_path / "PLOG_Tracker_Template.xlsx"
    dmr = tmp_path / "DMR_Export_Template.xlsx"
    parsed_plog = parse_plog(str(plog))
    assert len(parsed_plog.rows) == 2
    assert all(row.post_link.startswith("https://") for row in parsed_plog.rows)
    assert len(parse_dmr(str(dmr)).rows) == 1


def test_rubric_generator_has_pinned_dependency_and_valid_javascript():
    package = json.loads((TOOLS / "package.json").read_text())
    lock = json.loads((TOOLS / "package-lock.json").read_text())
    source = (TOOLS / "make_rubric.js").read_text()
    assert package["dependencies"]["docx"] == "9.7.1"
    assert lock["packages"][""]["dependencies"]["docx"] == "9.7.1"
    assert "Never mix units row by row" in source
    assert "≥1000K TOP · ≥400K MID · ≥200K BOT" in source
    assert "values <10,000" not in source
    assert 'eastAsia: "Arial Unicode MS"' in source
    assert "uploader explicitly consents" in source
    assert "otherwise it is rejected" in source

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    subprocess.run(
        [node, "--check", str(TOOLS / "make_rubric.js")],
        check=True,
        capture_output=True,
        text=True,
    )
