"""Finding the native ClickHouse binary for tests/test_user_study_clickhouse.py.

Locally, a missing binary or a server that won't start skips those tests.
In CI (.github/workflows/study-authz-tests.yml) the job sets
CBIOPORTAL_MCP_REQUIRE_CLICKHOUSE=1, which turns both into failures, so the
job can't pass with the real-ClickHouse checks silently skipped.

CBIOPORTAL_MCP_CLICKHOUSE_BIN optionally names the binary; otherwise
`clickhouse` is looked up on PATH.
"""

from __future__ import annotations

import os
import shutil

import pytest

REQUIRE_ENV = "CBIOPORTAL_MCP_REQUIRE_CLICKHOUSE"
BIN_ENV = "CBIOPORTAL_MCP_CLICKHOUSE_BIN"
_TRUE = {"1", "true", "yes", "on"}


def clickhouse_required() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in _TRUE


def clickhouse_binary() -> str | None:
    explicit = os.environ.get(BIN_ENV, "").strip()
    if explicit:
        # An explicit path that doesn't resolve counts as missing.
        return shutil.which(explicit)
    return shutil.which("clickhouse")


def clickhouse_unavailable(reason: str):
    """Skip (local default) or fail (CBIOPORTAL_MCP_REQUIRE_CLICKHOUSE set)."""
    if clickhouse_required():
        pytest.fail(f"{reason} ({REQUIRE_ENV} is set, so this is a failure)", pytrace=False)
    pytest.skip(reason)
