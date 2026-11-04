"""Strict shared env parsing and configuration gates across launch paths."""

import logging
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from cbioportal_mcp import rollout
from cbioportal_mcp.env import McpConfig, required_enum, required_list, validate_startup
from cbioportal_mcp.server import ConfiguredFastMCP


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for name in (
        "CBIOPORTAL_MCP_STUDY_FILTERING",
        "CBIOPORTAL_MCP_LOCAL_DEV",
        "CLICKHOUSE_MCP_SERVER_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("value", [None, "", " ", "false", "0", "disabled", "yes", "of"])
def test_filtering_rejects_missing_and_invalid(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", value)
    with pytest.raises(ValueError, match="CBIOPORTAL_MCP_STUDY_FILTERING"):
        validate_startup()


@pytest.mark.parametrize("value", ["on", " ON "])
def test_filtering_on_is_locked(monkeypatch, value):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", value)
    with pytest.raises(ValueError, match="cannot be enabled until a later release"):
        validate_startup()


@pytest.mark.parametrize("value", ["off", " OFF "])
def test_filtering_off_and_default_http(monkeypatch, value):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", value)
    assert validate_startup() == ("off", "http")
    assert McpConfig().mcp_server_transport == "http"


@pytest.mark.parametrize("transport", ["http", "sse", " HTTP "])
def test_explicit_network_transports(monkeypatch, transport):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", transport)
    assert validate_startup() == ("off", transport.strip().lower())


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", " 1 ", "1"])
def test_stdio_requires_exact_opt_in(monkeypatch, opt_in):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    if opt_in is not None:
        monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", opt_in)
    if opt_in == "1":
        assert validate_startup() == ("off", "stdio")
    else:
        with pytest.raises(ValueError, match="CBIOPORTAL_MCP_LOCAL_DEV=1"):
            validate_startup()


@pytest.mark.parametrize("choices", [("off", "enforce"), ("on", "off")])
def test_required_enum_is_generic(monkeypatch, choices):
    monkeypatch.setenv("TEST_SETTING", f" {choices[1].upper()} ")
    assert required_enum("TEST_SETTING", choices) == choices[1]
    monkeypatch.setenv("TEST_SETTING", "true")
    with pytest.raises(ValueError, match="TEST_SETTING must be one of"):
        required_enum("TEST_SETTING", choices)
    monkeypatch.delenv("TEST_SETTING")
    with pytest.raises(ValueError, match="TEST_SETTING is required"):
        required_enum("TEST_SETTING", choices)


@pytest.mark.parametrize(
    "value, expected",
    [("", []), ("  ", []), ("Alice", ["Alice"]), (" Alice, Bob ", ["Alice", "Bob"])],
)
def test_required_list(monkeypatch, value, expected):
    monkeypatch.setenv("TEST_USERS", value)
    assert required_list("TEST_USERS") == expected


@pytest.mark.parametrize("value", [None, ",", "a,", ",a", "a, ,b"])
def test_required_list_rejects_missing_and_empty_items(monkeypatch, value):
    monkeypatch.delenv("TEST_USERS", raising=False)
    if value is not None:
        monkeypatch.setenv("TEST_USERS", value)
    with pytest.raises(ValueError, match="TEST_USERS"):
        required_list("TEST_USERS")


@pytest.mark.parametrize("value, success", [(None, False), ("on", False), ("off", True)])
def test_non_main_module_import_validates(tmp_path, value, success):
    env = dict(os.environ)
    if value is not None:
        env["CBIOPORTAL_MCP_STUDY_FILTERING"] = value
    result = subprocess.run(
        [sys.executable, "-c", "from cbioportal_mcp.server import mcp; print(mcp.name)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (result.returncode == 0) == success, result.stderr
    if not success:
        assert "CBIOPORTAL_MCP_STUDY_FILTERING" in result.stderr


@pytest.mark.asyncio
async def test_launcher_cannot_override_to_stdio_without_opt_in(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    app = ConfiguredFastMCP(name="test")
    with patch.object(FastMCP, "run_stdio_async", new_callable=AsyncMock) as run:
        with pytest.raises(ValueError, match="CBIOPORTAL_MCP_LOCAL_DEV=1"):
            await app.run_stdio_async()
        run.assert_not_awaited()
        monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", "1")
        # Construction pinned rollout switches for HTTP; switching context
        # mid-process is refused (see test_rollout), so model a fresh process.
        rollout._reset_for_tests()
        await app.run_stdio_async()
        run.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_main_run_defaults_to_http(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    app = ConfiguredFastMCP(name="test")
    with patch.object(FastMCP, "run_async", new_callable=AsyncMock) as run:
        await app.run_async()
        run.assert_awaited_once_with(transport="http", show_banner=True)


@pytest.mark.asyncio
async def test_asgi_lifespan_revalidates_and_logs_effective_settings(monkeypatch, caplog):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    app = ConfiguredFastMCP(name="test")
    http = app.http_app(transport="sse")
    with caplog.at_level(logging.INFO, logger="cbioportal_mcp.server"):
        async with http.router.lifespan_context(http):
            pass
    assert caplog.text.count("study_filtering=off transport=sse") == 1
    monkeypatch.delenv("CBIOPORTAL_MCP_STUDY_FILTERING")
    with pytest.raises(ValueError, match="CBIOPORTAL_MCP_STUDY_FILTERING"):
        async with http.router.lifespan_context(http):
            pass


def test_invalid_transport_is_rejected(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "websocket")
    with pytest.raises(ValueError, match="Invalid transport"):
        validate_startup()


def test_server_construction_rejects_missing_configuration():
    with pytest.raises(ValueError, match="CBIOPORTAL_MCP_STUDY_FILTERING"):
        ConfiguredFastMCP(name="test")


@pytest.mark.asyncio
async def test_non_main_lifespan_checks_db_permissions(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    app = ConfiguredFastMCP(name="test").http_app()

    def deny(config):
        raise PermissionError("read-only grants required")

    monkeypatch.setattr("cbioportal_mcp.server.ensure_db_permissions", deny)
    with pytest.raises(SystemExit) as error:
        async with app.router.lifespan_context(app):
            pass
    assert error.value.code == 2
