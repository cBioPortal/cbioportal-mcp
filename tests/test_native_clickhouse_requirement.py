"""CBIOPORTAL_MCP_REQUIRE_CLICKHOUSE turns a missing native ClickHouse into a failure.

Locally (variable unset) the real-ClickHouse tests skip; the CI job sets it so
they can never pass by skipping. Runs without ClickHouse.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from native_clickhouse import (
    BIN_ENV,
    REQUIRE_ENV,
    clickhouse_binary,
    clickhouse_required,
    clickhouse_unavailable,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_truthy_values_require_clickhouse(monkeypatch, value):
    monkeypatch.setenv(REQUIRE_ENV, value)
    assert clickhouse_required()
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_ENV):
        clickhouse_unavailable("clickhouse binary not installed")


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "later"])
def test_otherwise_unavailable_clickhouse_skips(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(REQUIRE_ENV, raising=False)
    else:
        monkeypatch.setenv(REQUIRE_ENV, value)
    assert not clickhouse_required()
    with pytest.raises(pytest.skip.Exception, match="not installed"):
        clickhouse_unavailable("clickhouse binary not installed")


def test_explicit_binary_path(monkeypatch, tmp_path):
    fake = tmp_path / "clickhouse"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(BIN_ENV, str(fake))
    assert clickhouse_binary() == str(fake)
    # An explicit path that doesn't exist is "missing", not a PATH fallback.
    monkeypatch.setenv(BIN_ENV, str(tmp_path / "nope"))
    assert clickhouse_binary() is None


def _run_native_tests(tmp_path, require: str | None) -> subprocess.CompletedProcess:
    env = {
        "PATH": str(tmp_path),  # no clickhouse here...
        BIN_ENV: str(tmp_path / "missing-clickhouse"),  # ...nor here
        "HOME": str(tmp_path),
    }
    if require is not None:
        env[REQUIRE_ENV] = require
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"]
        + ["tests/test_user_study_clickhouse.py"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_native_suite_fails_instead_of_skipping_when_required(tmp_path):
    result = _run_native_tests(tmp_path, "1")
    assert result.returncode != 0, result.stdout
    assert "passed" not in result.stdout.splitlines()[-1]
    assert "skipped" not in result.stdout.splitlines()[-1]
    assert "clickhouse binary not installed" in result.stdout


def test_native_suite_skips_without_the_switch(tmp_path):
    result = _run_native_tests(tmp_path, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout.splitlines()[-1]
