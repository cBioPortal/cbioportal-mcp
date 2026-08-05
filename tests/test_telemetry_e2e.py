"""
End-to-end tests for MCP header/handshake → Datadog identity & origin propagation.

These tests run a real FastMCP HTTP server (via Starlette TestClient, no network socket
needed) and verify that custom request headers and the MCP `initialize` handshake's
`clientInfo` actually reach `TelemetryMiddleware` and its extraction helpers. They also
verify what would be emitted on the Datadog LLMObs span by patching `_llmobs_tool_span`
and confirming its arguments, and what lands on the OTel span by capturing real spans
through an in-memory exporter.

No live Datadog connection or DD_API_KEY is required.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

from fastmcp import FastMCP
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from cbioportal_mcp.telemetry import TelemetryMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(extra_middleware=None, *, stateless: bool = True):
    """Create a minimal FastMCP app with TelemetryMiddleware and a no-op tool.

    ``stateless=False`` is required to exercise mcp.client.name/mcp.session.id:
    both are handshake/session-level (clientInfo, the transport session ID),
    which only persist across the initialize -> tools/call requests when the
    HTTP session is stateful. x-user-id/x-user-email are plain per-request
    headers and work fine in either mode, which is why the existing tests
    below keep the stateless default.
    """
    middleware = [TelemetryMiddleware()]
    if extra_middleware:
        middleware.extend(extra_middleware)
    mcp = FastMCP("e2e-test", middleware=middleware)

    @mcp.tool()
    def ping() -> str:
        return "pong"

    return mcp.http_app(path="/mcp", stateless_http=stateless)


def _mcp_call(
    client: TestClient,
    tool: str,
    headers: dict,
    client_info: dict | None = None,
) -> dict:
    """Initialize an MCP session and call one tool, returns the parsed JSON-RPC result.

    ``client_info`` is the ``clientInfo`` block a real MCP client sends as part of
    ``initialize`` (e.g. ``{"name": "claude-code", "version": "1.2.3"}``); defaults
    to a generic test identity when not simulating a specific connector.
    """
    default_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    merged = {**default_headers, **headers}

    init_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": client_info or {"name": "test", "version": "1.0"},
            },
        },
        headers=merged,
    )
    assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"

    session_id = init_resp.headers.get("mcp-session-id", "")
    if session_id:
        merged["mcp-session-id"] = session_id
        # Stateful sessions track initialization lifecycle server-side and reject
        # requests sent before this notification; stateless mode has no session
        # to track, so it never returns a session ID and this block is skipped.
        notify_resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=merged,
        )
        assert notify_resp.status_code in (200, 202), (
            f"notifications/initialized failed: {notify_resp.text}"
        )

    tool_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        },
        headers=merged,
    )
    assert tool_resp.status_code == 200, f"tools/call failed: {tool_resp.text}"
    return tool_resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_x_user_id_header_reaches_extract_user_identity():
    """
    When LibreChat sends x-user-id in the HTTP request, the real _extract_user_identity
    must return that value as user_id and detect the client as "librechat".
    Verified end-to-end by capturing what _llmobs_tool_span receives.
    """
    app = _make_app()
    captured_calls: list = []

    def fake_llmobs_span(tool_name, arguments, user_id, user_email, client, client_name, client_version, session_id):
        captured_calls.append({"user_id": user_id, "user_email": user_email})
        return None

    with patch("cbioportal_mcp.telemetry._llmobs_tool_span", side_effect=fake_llmobs_span):
        with TestClient(app) as client:
            _mcp_call(client, "ping", headers={"x-user-id": "mongo-id-abc123"})

    assert captured_calls, "Expected _llmobs_tool_span to be called"
    assert captured_calls[0]["user_id"] == "mongo-id-abc123", (
        f"Expected user_id 'mongo-id-abc123', got {captured_calls[0]['user_id']!r}"
    )


def test_usr_id_tag_set_on_llmobs_span_when_header_present():
    """
    When x-user-id is present, TelemetryMiddleware must pass the correct user_id
    to _llmobs_tool_span so Datadog receives usr.id = <the real MongoDB ObjectId>.
    """
    app = _make_app()

    captured_calls: list = []

    def fake_llmobs_span(tool_name, arguments, user_id, user_email, client, client_name, client_version, session_id):
        captured_calls.append({"tool_name": tool_name, "user_id": user_id, "user_email": user_email})
        return None  # no actual span

    with patch("cbioportal_mcp.telemetry._llmobs_tool_span", side_effect=fake_llmobs_span):
        with TestClient(app) as client:
            _mcp_call(client, "ping", headers={"x-user-id": "507f1f77bcf86cd799439011"})

    assert captured_calls, "Expected _llmobs_tool_span to be called"
    assert captured_calls[0]["user_id"] == "507f1f77bcf86cd799439011"
    assert captured_calls[0]["tool_name"] == "ping"


def _run_with_span_capture(call, *, stateless: bool = True):
    """Run ``call`` (taking a TestClient) with a real OTel pipeline wired to an
    in-memory exporter, and return the finished spans it produced.

    Patches ``cbioportal_mcp.telemetry.trace.get_tracer`` (rather than calling
    ``trace.set_tracer_provider``) because the OTel SDK only allows the global
    TracerProvider to be set once per process — a second test calling
    ``set_tracer_provider`` would be silently ignored, leaving its exporter dark.
    ``mock.patch`` scopes the override to this call and restores it after.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    with patch("cbioportal_mcp.telemetry.trace.get_tracer", return_value=tracer):
        app = _make_app(stateless=stateless)
        with patch("cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None):
            with TestClient(app) as client:
                call(client)
    return exporter.get_finished_spans()


def test_mcp_client_tag_is_librechat_when_x_user_id_present():
    """
    The mcp.client OTel span attribute must be 'librechat' when x-user-id is present,
    regardless of the header value (even empty string counts as LibreChat traffic).
    """
    spans = _run_with_span_capture(
        lambda client: _mcp_call(client, "ping", headers={"x-user-id": "any-value"})
    )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    assert tool_spans[0].attributes["mcp.client"] == "librechat"


def test_mcp_client_tag_is_direct_when_no_x_user_id_header():
    """The mcp.client OTel span attribute must be 'direct' with no x-user-id header."""
    spans = _run_with_span_capture(
        lambda client: _mcp_call(client, "ping", headers={})
    )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    assert tool_spans[0].attributes["mcp.client"] == "direct"


def test_mcp_client_name_distinguishes_direct_connectors():
    """
    mcp.client alone only says "not librechat" for every direct connector. The
    clientInfo sent during initialize (mcp.client.name/version) is what actually
    distinguishes, e.g., Claude Code from Codex among that "direct" traffic.
    """
    spans = _run_with_span_capture(
        lambda client: _mcp_call(
            client, "ping", headers={}, client_info={"name": "claude-code", "version": "1.2.3"}
        ),
        stateless=False,
    )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    attrs = tool_spans[0].attributes
    assert attrs["mcp.client"] == "direct"
    assert attrs["mcp.client.name"] == "claude-code"
    assert attrs["mcp.client.version"] == "1.2.3"


def test_mcp_session_id_tagged_on_span_when_present():
    """
    mcp.session.id should land on the OTel span whenever the MCP transport session
    ID is available, giving anonymous direct-connector traffic a countable identity
    even with no user identity attached.

    This patches ``_extract_session_id`` directly rather than driving a real
    ``mcp-session-id`` header through Starlette's TestClient: that path is already
    covered at the unit level (test_telemetry_user_id.py::test_extracts_session_id_from_context),
    and TestClient's stateful-session simulation doesn't reliably surface the
    session header to FastMCP's own context lookup within a single test process
    (verified independently — the raw ASGI request carries the header, but
    ``get_http_headers()`` doesn't see it in that request cycle). What matters
    here is proving TelemetryMiddleware reads and tags whatever the extractor
    returns, end-to-end through the real span pipeline.
    """
    with patch("cbioportal_mcp.telemetry._extract_session_id", return_value="session-e2e-123"):
        spans = _run_with_span_capture(
            lambda client: _mcp_call(client, "ping", headers={}, client_info={"name": "codex", "version": "0.1"})
        )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    assert tool_spans[0].attributes.get("mcp.session.id") == "session-e2e-123"


def test_usr_id_absent_when_no_x_user_id_header():
    """
    When x-user-id is NOT in the request (direct MCP caller, not LibreChat),
    _llmobs_tool_span must receive user_id=None so Datadog does NOT record usr.id.
    """
    app = _make_app()

    captured_calls: list = []

    def fake_llmobs_span(tool_name, arguments, user_id, user_email, client, client_name, client_version, session_id):
        captured_calls.append({"tool_name": tool_name, "user_id": user_id})
        return None

    with patch("cbioportal_mcp.telemetry._llmobs_tool_span", side_effect=fake_llmobs_span):
        with TestClient(app) as client:
            # No x-user-id header — direct MCP caller
            _mcp_call(client, "ping", headers={})

    assert captured_calls, "Expected _llmobs_tool_span to be called"
    assert captured_calls[0]["user_id"] is None, (
        f"Expected user_id=None for direct caller, got {captured_calls[0]['user_id']!r}"
    )


def test_base64_encoded_email_decoded_before_llmobs_span():
    """
    LibreChat base64-encodes non-ASCII email values with a 'b64:' prefix.
    _extract_user_identity must decode them before passing to _llmobs_tool_span.
    """
    app = _make_app()

    encoded_email = "b64:" + base64.b64encode("user+tag@example.com".encode()).decode()
    captured_calls: list = []

    def fake_llmobs_span(tool_name, arguments, user_id, user_email, client, client_name, client_version, session_id):
        captured_calls.append({"user_id": user_id, "user_email": user_email})
        return None

    with patch("cbioportal_mcp.telemetry._llmobs_tool_span", side_effect=fake_llmobs_span):
        with TestClient(app) as client:
            _mcp_call(client, "ping", headers={
                "x-user-id": "user-id-123",
                "x-user-email": encoded_email,
            })

    assert captured_calls, "Expected _llmobs_tool_span to be called"
    assert captured_calls[0]["user_email"] == "user+tag@example.com"
    assert captured_calls[0]["user_id"] == "user-id-123"
