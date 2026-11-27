# Import the package before test modules import mcp_clickhouse: the package
# sizes mcp-clickhouse's query pool before it is created (see query_concurrency).
import os

import pytest

# Server imports during collection now require an explicit deployment choice.
os.environ["CBIOPORTAL_MCP_STUDY_FILTERING"] = "off"
# Rollout switches are required in HTTP mode too (see cbioportal_mcp.rollout).
os.environ["CBIOPORTAL_MCP_AUTH_ENFORCEMENT"] = "off"
os.environ["CBIOPORTAL_MCP_AUTH_TEST_USERS"] = ""
os.environ["CBIOPORTAL_MCP_PUBLIC_DB_CHECK"] = "off"

import cbioportal_mcp  # noqa: F401
from cbioportal_mcp import result_format, rollout


@pytest.fixture(autouse=True)
def _default_result_format(monkeypatch):
    """Tests assume the default result format unless they set one themselves."""
    for name in (
        result_format.RESULT_FORMAT_ENV,
        result_format.MAX_RESULT_ROWS_ENV,
        result_format.MAX_CELL_CHARS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_rollout_pin():
    """Each test is its own "process" for the parse-once rollout pin."""
    rollout._reset_for_tests()
    yield
    rollout._reset_for_tests()


@pytest.fixture(autouse=True)
def _mock_startup_db_permissions(monkeypatch):
    """Unit tests use fake data; exercise the real permission gate separately."""
    monkeypatch.setattr("cbioportal_mcp.server.ensure_db_permissions", lambda config: None)


@pytest.fixture(autouse=True)
def _fresh_study_availability():
    from cbioportal_mcp.study_authz import availability
    availability.complete("checks_pending")
    yield
    availability.complete("checks_pending")
