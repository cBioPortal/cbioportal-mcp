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
import json
from unittest.mock import patch

import pytest

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.middleware import Middleware
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

from cbioportal_mcp.telemetry import TelemetryMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sse_exit_event():
    """sse_starlette caches a process-global ``anyio.Event`` bound to the first
    event loop that streams a response. Each TestClient runs its own loop, so
    without this reset every test after the first gets an empty SSE body (the
    stream errors with "bound to a different event loop") while still returning
    HTTP 200 — hiding the actual JSON-RPC response from assertions."""
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


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


def _parse_jsonrpc_response(resp) -> dict:
    """Parse a Streamable HTTP response body (plain JSON or SSE) into the JSON-RPC
    message. JSON-RPC errors are also delivered with HTTP 200, so callers must
    inspect the payload rather than the status code alone."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data_lines = [
            line[len("data:"):].strip()
            for line in resp.text.splitlines()
            if line.startswith("data:")
        ]
        assert data_lines, f"No SSE data line in response: {resp.text!r}"
        return json.loads(data_lines[-1])
    return resp.json()


def _mcp_discovery_call(
    client: TestClient,
    method: str,
    headers: dict,
    client_info: dict | None = None,
    *,
    expect_success: bool = True,
) -> dict:
    """Initialize an MCP session and send one discovery-only request
    (``tools/list`` / ``resources/list`` / ``prompts/list``), *without* ever
    calling ``tools/call``.

    This is the connector setup / capability-negotiation traffic a client sends
    right after ``initialize`` — the population the discovery hooks
    (on_list_tools/on_list_resources/on_list_prompts) exist to make visible,
    since it never reaches on_call_tool.

    Returns the parsed JSON-RPC response. With ``expect_success`` (the default)
    asserts it is a successful ``result`` with no ``error``.
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
        notify_resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=merged,
        )
        assert notify_resp.status_code in (200, 202), (
            f"notifications/initialized failed: {notify_resp.text}"
        )

    list_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": {}},
        headers=merged,
    )
    assert list_resp.status_code == 200, f"{method} failed: {list_resp.text}"
    payload = _parse_jsonrpc_response(list_resp)
    if expect_success:
        assert "error" not in payload, f"{method} returned a JSON-RPC error: {payload}"
        assert "result" in payload, f"{method} returned no JSON-RPC result: {payload}"
    return payload


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


def _run_with_span_capture(
    call,
    *,
    stateless: bool = True,
    extra_processors: tuple[SpanProcessor, ...] = (),
    extra_middleware: list | None = None,
):
    """Run ``call`` (taking a TestClient) with a real OTel pipeline wired to an
    in-memory exporter, and return the finished spans it produced.

    Patches ``cbioportal_mcp.telemetry.trace.get_tracer`` (rather than calling
    ``trace.set_tracer_provider``) because the OTel SDK only allows the global
    TracerProvider to be set once per process — a second test calling
    ``set_tracer_provider`` would be silently ignored, leaving its exporter dark.
    ``mock.patch`` scopes the override to this call and restores it after.

    ``extra_processors`` are registered before the capturing exporter (e.g. a
    processor that raises); ``extra_middleware`` runs inside TelemetryMiddleware.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    for processor in extra_processors:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    with patch("cbioportal_mcp.telemetry.trace.get_tracer", return_value=tracer):
        app = _make_app(extra_middleware, stateless=stateless)
        with patch("cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None):
            with TestClient(app) as client:
                call(client)
    return exporter.get_finished_spans()


def test_mcp_client_tag_is_librechat_when_x_user_id_present():
    """
    The mcp.client_kind OTel span attribute must be 'librechat' when x-user-id is
    present, regardless of the header value (even empty string counts as LibreChat
    traffic).
    """
    spans = _run_with_span_capture(
        lambda client: _mcp_call(client, "ping", headers={"x-user-id": "any-value"})
    )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    assert tool_spans[0].attributes["mcp.client_kind"] == "librechat"


def test_mcp_client_tag_is_direct_when_no_x_user_id_header():
    """The mcp.client_kind OTel span attribute must be 'direct' with no x-user-id header."""
    spans = _run_with_span_capture(
        lambda client: _mcp_call(client, "ping", headers={})
    )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    assert tool_spans[0].attributes["mcp.client_kind"] == "direct"


def test_mcp_client_tag_is_oauth_when_access_token_present():
    """
    A caller with a verified OAuth access token (this deployment has OAuth
    enabled and the caller completed a real Keycloak login) must be tagged
    mcp.client_kind = "oauth" with enduser.id from the token's sub claim —
    the strongest identity tier, distinct from the trusted-but-unverified
    x-user-id header path.
    """
    token = AccessToken(
        token="fake-token",
        client_id="fake-client",
        scopes=[],
        claims={"sub": "keycloak-user-abc", "email": "alice@example.org"},
    )
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        spans = _run_with_span_capture(
            lambda client: _mcp_call(client, "ping", headers={})
        )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    attrs = tool_spans[0].attributes
    assert attrs["mcp.client_kind"] == "oauth"
    assert attrs["enduser.id"] == "keycloak-user-abc"


def test_oauth_identity_takes_precedence_over_x_user_id_header():
    """
    When both a verified OAuth token and the trusted-but-unverified x-user-id
    header are present on the same request, the verified identity must win.
    """
    token = AccessToken(
        token="fake-token",
        client_id="fake-client",
        scopes=[],
        claims={"sub": "keycloak-user-abc"},
    )
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        spans = _run_with_span_capture(
            lambda client: _mcp_call(client, "ping", headers={"x-user-id": "librechat-user-1"})
        )

    tool_spans = [s for s in spans if s.name == "mcp.tool/ping"]
    assert tool_spans, "Expected an mcp.tool/ping span"
    attrs = tool_spans[0].attributes
    assert attrs["mcp.client_kind"] == "oauth"
    assert attrs["enduser.id"] == "keycloak-user-abc"


def test_mcp_client_name_distinguishes_direct_connectors():
    """
    mcp.client_kind alone only says "not librechat" for every direct connector. The
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
    assert attrs["mcp.client_kind"] == "direct"
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


def test_tools_list_produces_span_without_any_tool_call():
    """
    A connector that only completes initialize -> tools/list (real-world
    "connector setup" traffic that never invokes a tool) must still produce
    an OTel span with client identity attributes — this is exactly the
    population that previously produced zero telemetry of any kind.
    """
    spans = _run_with_span_capture(
        lambda client: _mcp_discovery_call(
            client,
            "tools/list",
            headers={},
            client_info={"name": "claude-code", "version": "1.2.3"},
        ),
        stateless=False,
    )

    tool_spans = [s for s in spans if s.name.startswith("mcp.tool/")]
    assert not tool_spans, "No tools/call happened; there must be no mcp.tool/* span"

    discovery_spans = [s for s in spans if s.name == "mcp.discovery/tools_list"]
    assert discovery_spans, "Expected an mcp.discovery/tools_list span"
    attrs = discovery_spans[0].attributes
    assert attrs["mcp.client_kind"] == "direct"
    assert attrs["mcp.client.name"] == "claude-code"
    assert attrs["mcp.client.version"] == "1.2.3"


def test_resources_list_produces_discovery_span():
    spans = _run_with_span_capture(
        lambda client: _mcp_discovery_call(
            client, "resources/list", headers={"x-user-id": "librechat-user-1"}
        )
    )

    discovery_spans = [s for s in spans if s.name == "mcp.discovery/resources_list"]
    assert discovery_spans, "Expected an mcp.discovery/resources_list span"
    attrs = discovery_spans[0].attributes
    assert attrs["mcp.client_kind"] == "librechat"
    assert attrs["enduser.id"] == "librechat-user-1"


def test_prompts_list_produces_discovery_span():
    spans = _run_with_span_capture(
        lambda client: _mcp_discovery_call(client, "prompts/list", headers={})
    )

    discovery_spans = [s for s in spans if s.name == "mcp.discovery/prompts_list"]
    assert discovery_spans, "Expected an mcp.discovery/prompts_list span"
    assert discovery_spans[0].attributes["mcp.client_kind"] == "direct"


def test_discovery_span_carries_oauth_resolved_identity():
    """
    Discovery spans must resolve identity exactly like tool-call spans: a
    verified OAuth token wins over the unverified x-user-id header, and the
    token's email claim lands on enduser.email.
    """
    token = AccessToken(
        token="fake-token",
        client_id="fake-client",
        scopes=[],
        claims={"sub": "keycloak-user-abc", "email": "alice@example.org"},
    )
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        spans = _run_with_span_capture(
            lambda client: _mcp_discovery_call(
                client, "tools/list", headers={"x-user-id": "librechat-user-1"}
            )
        )

    discovery_spans = [s for s in spans if s.name == "mcp.discovery/tools_list"]
    assert discovery_spans, "Expected an mcp.discovery/tools_list span"
    attrs = discovery_spans[0].attributes
    assert attrs["mcp.client_kind"] == "oauth"
    assert attrs["enduser.id"] == "keycloak-user-abc"
    assert attrs["enduser.email"] == "alice@example.org"


def test_discovery_requests_do_not_start_llmobs_span():
    """Discovery requests aren't tool calls, so no LLMObs tool span is started."""
    app = _make_app()
    with patch("cbioportal_mcp.telemetry._llmobs_tool_span") as llmobs_span:
        with TestClient(app) as client:
            for method in ("tools/list", "resources/list", "prompts/list"):
                _mcp_discovery_call(client, method, headers={})

    llmobs_span.assert_not_called()


_DISCOVERY_METHODS = ("tools/list", "resources/list", "prompts/list")


class _RaisingSpanProcessor(SpanProcessor):
    """Span processor that fails in on_start and/or on_end, simulating a broken
    telemetry pipeline."""

    def __init__(self, *, fail_on_start: bool, fail_on_end: bool) -> None:
        self._fail_on_start = fail_on_start
        self._fail_on_end = fail_on_end

    def on_start(self, span, parent_context=None) -> None:
        if self._fail_on_start:
            raise RuntimeError("telemetry processor unavailable")

    def on_end(self, span) -> None:
        if self._fail_on_end:
            raise RuntimeError("telemetry processor unavailable")


class _DiscoveryCallCounter(Middleware):
    """Inner middleware recording each discovery request that reaches the handler."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_list_tools(self, context, call_next):
        self.calls.append("tools/list")
        return await call_next(context)

    async def on_list_resources(self, context, call_next):
        self.calls.append("resources/list")
        return await call_next(context)

    async def on_list_prompts(self, context, call_next):
        self.calls.append("prompts/list")
        return await call_next(context)


class _FailingToolsList(Middleware):
    """Inner middleware whose tools/list handler raises."""

    async def on_list_tools(self, context, call_next):
        raise ValueError("downstream tools/list failure")


@pytest.mark.parametrize(
    "fail_on_start, fail_on_end",
    [(True, False), (False, True), (True, True)],
    ids=["on_start", "on_end", "on_start_and_on_end"],
)
def test_discovery_requests_survive_failing_span_processor(fail_on_start, fail_on_end):
    """
    Telemetry is best-effort: a span processor that raises must never fail a
    discovery request or replace its result, and the downstream handler must
    run exactly once per request.
    """
    counter = _DiscoveryCallCounter()
    payloads: list[dict] = []

    def run(client):
        for method in _DISCOVERY_METHODS:
            payloads.append(_mcp_discovery_call(client, method, headers={}))

    _run_with_span_capture(
        run,
        extra_processors=(
            _RaisingSpanProcessor(fail_on_start=fail_on_start, fail_on_end=fail_on_end),
        ),
        extra_middleware=[counter],
    )

    assert len(payloads) == len(_DISCOVERY_METHODS)
    assert any(t["name"] == "ping" for t in payloads[0]["result"]["tools"])
    assert counter.calls == list(_DISCOVERY_METHODS)


def test_discovery_handler_exception_still_propagates():
    """
    An exception from the downstream handler is not swallowed by the telemetry
    guarding: the client gets a JSON-RPC error and the span is marked as failed.
    """
    payloads: list[dict] = []
    spans = _run_with_span_capture(
        lambda client: payloads.append(
            _mcp_discovery_call(client, "tools/list", headers={}, expect_success=False)
        ),
        extra_middleware=[_FailingToolsList()],
    )

    assert "error" in payloads[0], f"Expected a JSON-RPC error, got {payloads[0]}"
    assert "result" not in payloads[0]

    discovery_spans = [s for s in spans if s.name == "mcp.discovery/tools_list"]
    assert discovery_spans, "Expected an mcp.discovery/tools_list span"
    span = discovery_spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"
