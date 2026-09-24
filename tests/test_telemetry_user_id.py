import base64
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastmcp.server.auth import AccessToken
from mcp.types import Implementation

from cbioportal_mcp.telemetry import (
    TelemetryMiddleware,
    _DogStatsDClient,
    _emit_db_query_metrics,
    _emit_tool_metrics,
    _extract_mcp_client_info,
    _extract_oauth_identity,
    _extract_session_id,
    _extract_user_identity,
    _resolve_caller_identity,
    _sanitize_datadog_tag_value,
    dogstatsd_metrics_configured,
    traced_db_query,
)


def _access_token(**claims) -> AccessToken:
    return AccessToken(token="fake-token", client_id="fake-client", scopes=[], claims=claims)


def test_extracts_user_id_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "507f1f77bcf86cd799439011"}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id == "507f1f77bcf86cd799439011"
        assert user_email is None
        assert client == "librechat"


def test_extracts_user_email_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "abc123", "x-user-email": "user@example.com"}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id == "abc123"
        assert user_email == "user@example.com"
        assert client == "librechat"


def test_decodes_base64_email():
    encoded = "b64:" + base64.b64encode(b"user+tag@example.com").decode()
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-email": encoded}):
        _, user_email, client = _extract_user_identity()
        assert user_email == "user+tag@example.com"
        assert client == "direct"


def test_returns_none_when_headers_absent():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id is None
        assert user_email is None
        assert client == "direct"


def test_returns_none_when_empty_string():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "", "x-user-email": ""}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id is None
        assert user_email is None
        assert client == "librechat"


def _context_with_client_info(name: str, version: str) -> Mock:
    context = Mock()
    context.fastmcp_context.session.client_params.clientInfo = Implementation(
        name=name, version=version
    )
    return context


def test_extracts_mcp_client_info_for_claude_code():
    context = _context_with_client_info("claude-code", "1.2.3")
    name, version = _extract_mcp_client_info(context)
    assert name == "claude-code"
    assert version == "1.2.3"


def test_extracts_mcp_client_info_for_codex():
    context = _context_with_client_info("codex", "0.7.9")
    name, version = _extract_mcp_client_info(context)
    assert name == "codex"
    assert version == "0.7.9"


def test_client_info_none_when_no_fastmcp_context():
    context = Mock()
    context.fastmcp_context = None
    name, version = _extract_mcp_client_info(context)
    assert name is None
    assert version is None


def test_client_info_none_when_client_params_missing():
    context = Mock()
    context.fastmcp_context.session.client_params = None
    name, version = _extract_mcp_client_info(context)
    assert name is None
    assert version is None


def test_extracts_session_id_from_context():
    context = Mock()
    context.fastmcp_context.session_id = "session-abc-123"
    assert _extract_session_id(context) == "session-abc-123"


def test_session_id_none_when_no_fastmcp_context():
    context = Mock()
    context.fastmcp_context = None
    assert _extract_session_id(context) is None


def test_extracts_oauth_identity_from_access_token():
    token = _access_token(sub="keycloak-user-abc", email="alice@example.org")
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        user_id, user_email = _extract_oauth_identity()
        assert user_id == "keycloak-user-abc"
        assert user_email == "alice@example.org"


def test_oauth_identity_none_when_no_access_token():
    with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
        user_id, user_email = _extract_oauth_identity()
        assert user_id is None
        assert user_email is None


def test_oauth_identity_none_when_sub_claim_missing():
    token = _access_token(email="alice@example.org")
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        user_id, user_email = _extract_oauth_identity()
        assert user_id is None
        assert user_email == "alice@example.org"


def test_resolve_caller_identity_prefers_oauth_over_header():
    token = _access_token(sub="keycloak-user-abc", email="alice@example.org")
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=token),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"x-user-id": "librechat-user-1"},
        ),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id == "keycloak-user-abc"
        assert user_email == "alice@example.org"
        assert client == "oauth"


def test_resolve_caller_identity_falls_back_to_header_when_no_oauth_token():
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"x-user-id": "librechat-user-1"},
        ),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id == "librechat-user-1"
        assert client == "librechat"


def test_resolve_caller_identity_direct_when_neither_present():
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch("fastmcp.server.dependencies.get_http_headers", return_value={}),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id is None
        assert client == "direct"


def test_dogstatsd_metrics_configured_when_agent_host_present(monkeypatch):
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.1")
    monkeypatch.delenv("CBIOPORTAL_MCP_DD_METRICS_ENABLED", raising=False)

    assert dogstatsd_metrics_configured() is True


def test_dogstatsd_metrics_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.1")
    monkeypatch.setenv("CBIOPORTAL_MCP_DD_METRICS_ENABLED", "false")

    assert dogstatsd_metrics_configured() is False


def test_sanitizes_datadog_tag_values():
    assert (
        _sanitize_datadog_tag_value("ClickHouse Run Select Query")
        == "clickhouse_run_select_query"
    )
    assert _sanitize_datadog_tag_value("Claude Code/1.2") == "claude_code/1.2"
    assert _sanitize_datadog_tag_value("") == "unknown"


def test_dogstatsd_client_emits_counter_and_distribution_packets():
    sent_packets = []

    class FakeSocket:
        def sendto(self, packet, address):
            sent_packets.append((packet.decode("utf-8"), address))

    with patch("socket.socket", return_value=FakeSocket()):
        client = _DogStatsDClient(
            "127.0.0.1",
            8125,
            prefix="cbioportal_mcp",
            constant_tags={"service": "cbioportal-mcp", "env": "prod"},
        )
        client.increment("tool.calls", {"tool": "list_studies", "success": "true"})
        client.distribution("tool.duration_ms", 12.345, {"tool": "list_studies"})

    assert sent_packets == [
        (
            "cbioportal_mcp.tool.calls:1|c|#env:prod,service:cbioportal-mcp,success:true,tool:list_studies",
            ("127.0.0.1", 8125),
        ),
        (
            "cbioportal_mcp.tool.duration_ms:12.345|d|#env:prod,service:cbioportal-mcp,tool:list_studies",
            ("127.0.0.1", 8125),
        ),
    ]


def test_emit_tool_metrics_tags_call_latency_and_errors():
    calls = []

    class FakeDogStatsD:
        def increment(self, metric, tags):
            calls.append(("increment", metric, tags))

        def distribution(self, metric, value, tags):
            calls.append(("distribution", metric, value, tags))

    with patch("cbioportal_mcp.telemetry._get_dogstatsd_client", return_value=FakeDogStatsD()):
        _emit_tool_metrics(
            tool_name="list_studies",
            duration_ms=12.34567,
            success=False,
            client_kind="direct",
            client_name="claude-code",
        )

    assert calls == [
        (
            "increment",
            "tool.calls",
            {
                "tool": "list_studies",
                "success": "false",
                "client_kind": "direct",
                "client_name": "claude-code",
            },
        ),
        (
            "distribution",
            "tool.duration_ms",
            12.346,
            {
                "tool": "list_studies",
                "success": "false",
                "client_kind": "direct",
                "client_name": "claude-code",
            },
        ),
        (
            "increment",
            "tool.errors",
            {
                "tool": "list_studies",
                "success": "false",
                "client_kind": "direct",
                "client_name": "claude-code",
            },
        ),
    ]


def test_emit_db_query_metrics_tags_call_latency_and_errors():
    calls = []

    class FakeDogStatsD:
        def increment(self, metric, tags):
            calls.append(("increment", metric, tags))

        def distribution(self, metric, value, tags):
            calls.append(("distribution", metric, value, tags))

    with patch("cbioportal_mcp.telemetry._get_dogstatsd_client", return_value=FakeDogStatsD()):
        _emit_db_query_metrics(query_label="study_guide.counts", duration_ms=8.1234, success=False)

    assert calls == [
        ("increment", "db_query.calls", {"query_label": "study_guide.counts", "success": "false"}),
        (
            "distribution",
            "db_query.duration_ms",
            8.123,
            {"query_label": "study_guide.counts", "success": "false"},
        ),
        ("increment", "db_query.errors", {"query_label": "study_guide.counts", "success": "false"}),
    ]


def test_traced_db_query_emits_success_metrics_and_no_error_tag():
    calls = []

    class FakeDogStatsD:
        def increment(self, metric, tags):
            calls.append(("increment", metric, tags))

        def distribution(self, metric, value, tags):
            calls.append(("distribution", metric, value, tags))

    with patch("cbioportal_mcp.telemetry._get_dogstatsd_client", return_value=FakeDogStatsD()):
        with traced_db_query("study_guide.study_info"):
            pass

    tags = {"query_label": "study_guide.study_info", "success": "true"}
    assert calls == [
        ("increment", "db_query.calls", tags),
        ("distribution", "db_query.duration_ms", calls[1][2], tags),
    ]


def test_traced_db_query_reraises_and_tags_the_failure():
    calls = []

    class FakeDogStatsD:
        def increment(self, metric, tags):
            calls.append(("increment", metric, tags))

        def distribution(self, metric, value, tags):
            calls.append(("distribution", metric, value, tags))

    with patch("cbioportal_mcp.telemetry._get_dogstatsd_client", return_value=FakeDogStatsD()):
        with pytest.raises(RuntimeError, match="clickhouse unavailable"):
            with traced_db_query("study_guide.counts"):
                raise RuntimeError("clickhouse unavailable")

    error_tags = {"query_label": "study_guide.counts", "success": "false"}
    assert ("increment", "db_query.errors", error_tags) in calls


@pytest.mark.asyncio
async def test_telemetry_middleware_emits_success_tool_metrics():
    emitted = []
    middleware = TelemetryMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="ping", arguments={}),
        fastmcp_context=None,
    )

    async def call_next(_context):
        return "pong"

    with (
        patch(
            "cbioportal_mcp.telemetry._resolve_caller_identity",
            return_value=(None, None, "direct"),
        ),
        patch("cbioportal_mcp.telemetry._extract_mcp_client_info", return_value=("codex", "0.1")),
        patch("cbioportal_mcp.telemetry._extract_session_id", return_value=None),
        patch("cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None),
        patch(
            "cbioportal_mcp.telemetry._emit_tool_metrics",
            side_effect=lambda **kwargs: emitted.append(kwargs),
        ),
    ):
        result = await middleware.on_call_tool(context, call_next)

    assert result == "pong"
    assert emitted[0]["tool_name"] == "ping"
    assert emitted[0]["success"] is True
    assert emitted[0]["client_kind"] == "direct"
    assert emitted[0]["client_name"] == "codex"
    assert emitted[0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_telemetry_middleware_emits_failure_tool_metrics():
    emitted = []
    middleware = TelemetryMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="ping", arguments={}),
        fastmcp_context=None,
    )

    async def call_next(_context):
        raise RuntimeError("boom")

    with (
        patch(
            "cbioportal_mcp.telemetry._resolve_caller_identity",
            return_value=(None, None, "direct"),
        ),
        patch("cbioportal_mcp.telemetry._extract_mcp_client_info", return_value=(None, None)),
        patch("cbioportal_mcp.telemetry._extract_session_id", return_value=None),
        patch("cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None),
        patch(
            "cbioportal_mcp.telemetry._emit_tool_metrics",
            side_effect=lambda **kwargs: emitted.append(kwargs),
        ),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await middleware.on_call_tool(context, call_next)

    assert emitted[0]["tool_name"] == "ping"
    assert emitted[0]["success"] is False
    assert emitted[0]["client_kind"] == "direct"
    assert emitted[0]["client_name"] is None
    assert emitted[0]["duration_ms"] >= 0
