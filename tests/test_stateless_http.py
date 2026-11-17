"""The HTTP transport runs stateless by default so a restart can't strand open MCP sessions."""

import logging
import types

import pytest

from cbioportal_mcp import server
from cbioportal_mcp.env import get_mcp_config


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("true", True), ("1", True), ("false", False), ("0", False), ("No", False), ("off", False)],
)
def test_stateless_http_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CLICKHOUSE_MCP_STATELESS_HTTP", raising=False)
    else:
        monkeypatch.setenv("CLICKHOUSE_MCP_STATELESS_HTTP", value)
    assert get_mcp_config().mcp_stateless_http is expected


def _run_main(monkeypatch, transport, stateless):
    seen = {}
    config = types.SimpleNamespace(
        mcp_server_transport=transport,
        mcp_bind_host="0.0.0.0",
        mcp_bind_port=8000,
        mcp_http_path="/db/mcp",
        mcp_forwarded_allow_ips=None,
        mcp_stateless_http=stateless,
    )
    monkeypatch.setattr(server, "get_mcp_config", lambda: config)
    monkeypatch.setattr(server.query_concurrency, "verify_query_pool", lambda n: None)
    monkeypatch.setattr(server.query_concurrency, "query_pool_size", lambda: 1)
    monkeypatch.setattr(server, "ensure_db_permissions", lambda config: None)
    monkeypatch.setattr(server, "_refresh_studies_cache_once", lambda: None)
    monkeypatch.setattr(server, "_refresh_studies_cache_forever", lambda: None)
    monkeypatch.setattr(server, "configure_telemetry", lambda: None)
    monkeypatch.setattr(server, "dogstatsd_metrics_configured", lambda: False)
    monkeypatch.setattr(server.mcp, "run", lambda **kw: seen.update(kw))
    server.main()
    return seen


def test_http_transport_is_stateless_by_default(monkeypatch):
    assert _run_main(monkeypatch, "http", True)["stateless_http"] is True


def test_stateless_can_be_turned_off(monkeypatch):
    assert "stateless_http" not in _run_main(monkeypatch, "http", False)


def test_sse_transport_is_unaffected(monkeypatch):
    assert "stateless_http" not in _run_main(monkeypatch, "sse", True)


def test_httpx_request_urls_are_not_logged(monkeypatch):
    _run_main(monkeypatch, "http", True)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
