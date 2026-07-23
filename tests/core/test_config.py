"""Fail-fast validation for operational environment settings."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app import config


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("TIKHUB_TIMEOUT", "0", "finite number > 0"),
        ("TIKHUB_TIMEOUT", "nan", "finite number > 0"),
        ("DIRECT_HTTP_TIMEOUT", "-1", "finite number > 0"),
        ("DIRECT_HTTP_TIMEOUT", "inf", "finite number > 0"),
        ("TIKHUB_MAX_RETRIES", "-1", "must be >= 0"),
        ("DIRECT_HTTP_RETRIES", "-1", "must be >= 0"),
        ("CANDIDATE_DATE_WINDOW_DAYS", "-1", "must be >= 0"),
        ("FAILED_CACHE_TTL_HOURS", "0", "must be >= 1"),
    ],
)
def test_invalid_operational_setting_fails_during_import(
        name, value, message):
    env = {**os.environ, name: value}

    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert name in result.stderr
    assert message in result.stderr


def test_operational_setting_defaults_and_zero_semantics(monkeypatch):
    for name in (
        "TIKHUB_TIMEOUT",
        "DIRECT_HTTP_TIMEOUT",
        "TIKHUB_MAX_RETRIES",
        "DIRECT_HTTP_RETRIES",
        "CANDIDATE_DATE_WINDOW_DAYS",
        "FAILED_CACHE_TTL_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config._positive_finite_float_env("TIKHUB_TIMEOUT", "15") == 15
    assert config._positive_finite_float_env("DIRECT_HTTP_TIMEOUT", "3") == 3
    assert config._nonnegative_int_env("TIKHUB_MAX_RETRIES", "3") == 3
    assert config._nonnegative_int_env("DIRECT_HTTP_RETRIES", "0") == 0
    assert config._nonnegative_int_env(
        "CANDIDATE_DATE_WINDOW_DAYS", "7"
    ) == 7
    assert config._positive_int_env("FAILED_CACHE_TTL_HOURS", "24") == 24

    monkeypatch.setenv("TIKHUB_MAX_RETRIES", "0")
    monkeypatch.setenv("CANDIDATE_DATE_WINDOW_DAYS", "0")
    assert config._nonnegative_int_env("TIKHUB_MAX_RETRIES", "3") == 0
    assert config._nonnegative_int_env(
        "CANDIDATE_DATE_WINDOW_DAYS", "7"
    ) == 0
