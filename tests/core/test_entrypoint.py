"""Deployment entrypoint safety checks."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


def test_entrypoint_is_valid_posix_shell():
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)


def test_root_entrypoint_refuses_filesystem_root(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text("#!/bin/sh\necho 0\n")
    fake_id.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "DATA_DIR": "/"}
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "true"], env=env,
        text=True, capture_output=True,
    )
    assert result.returncode == 64
    assert "Refusing unsafe DATA_DIR" in result.stderr


def test_entrypoint_uses_versioned_ownership_marker():
    source = ENTRYPOINT.read_text()
    assert ".ownership-appuser-10001-v1" in source
    assert 'if [ ! -f "$ownership_marker" ]' in source
    assert '[ -L "$ownership_marker" ]' in source
    assert 'chown appuser:appuser "$data_dir" "$ownership_marker"' not in source


def test_container_inputs_are_immutable_and_hash_checked():
    source = DOCKERFILE.read_text()
    assert "FROM python:3.12.11-slim-bookworm@sha256:" in source
    assert "pip install --no-cache-dir --require-hashes" in source
    lock = (ROOT / "requirements.lock").read_text()
    assert "--hash=sha256:" in lock


def test_sensitive_runtime_data_is_excluded_from_build_context():
    patterns = {
        line for line in DOCKERIGNORE.read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert patterns == {
        "*", "!app/", "!app/**", "!requirements.txt",
        "!requirements.lock", "!entrypoint.sh",
    }
