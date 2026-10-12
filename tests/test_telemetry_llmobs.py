"""
TelemetryMiddleware → Datadog LLM Observability tool spans, through the REAL ddtrace
LLMObs SDK (not a stub of ``_llmobs_tool_span``).

Two capture modes, neither of which sends anything anywhere:

- ``llmobs_events``: agent-proxy mode with APM tracing disabled, so every finished
  LLMObs span is handed to the LLMObs span writer as a fully-assembled span event;
  the writer's ``enqueue`` is replaced with a list append.
- ``prod_mode_spans``: the production configuration from server.py (agentless, APM
  tracing on). Here ddtrace ships the LLMObs payload on the APM trace, so the APM
  trace writer's ``write`` is replaced with a list append. A fake API key is used
  and outbound HTTP connections are refused (and recorded) for the duration.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
from unittest.mock import patch

import anyio
import mcp.types as mt
import pytest
from ddtrace.llmobs import LLMObs
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

from cbioportal_mcp import telemetry
from cbioportal_mcp.telemetry import TelemetryMiddleware


@pytest.fixture(autouse=True)
def _reset_sse_exit_event():
    """See the identically named fixture in test_telemetry_e2e.py."""
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


@pytest.fixture(autouse=True)
def _reset_llmobs_warning():
    telemetry._llmobs_failure_logged = False
    yield
    telemetry._llmobs_failure_logged = False


@pytest.fixture
def llmobs_events(monkeypatch):
    """Enable the real LLMObs SDK and capture the span events it would export."""
    # Route finished LLMObs spans straight to the LLMObs span writer (instead of
    # riding an APM trace) and drop the APM trace itself.
    monkeypatch.setenv("DD_APM_TRACING_ENABLED", "false")
    assert not LLMObs.enabled
    LLMObs.enable(
        agent_service="cbioportal-mcp-test",
        agentless_enabled=False,
        integrations_enabled=False,
    )
    events: list[dict] = []
    monkeypatch.setattr(LLMObs._instance._llmobs_span_writer, "enqueue", events.append)
    try:
        yield events
    finally:
        LLMObs.disable()


@pytest.fixture
def prod_mode_spans(monkeypatch):
    """Enable LLMObs exactly as server.py does in production (agentless, APM tracing
    on) and capture the finished APM spans, which carry the LLMObs payload."""
    import ddtrace
    from ddtrace.llmobs import _llmobs

    attempted: list[str] = []

    def _refuse(self, *args, **kwargs):
        attempted.append(f"{self.host}:{self.port}")
        raise ConnectionRefusedError("network disabled in tests")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", _refuse)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _refuse)
    # Keep ddtrace's instrumentation telemetry on its existing (local agent)
    # client rather than switching it to the Datadog intake with the fake key.
    monkeypatch.setattr(_llmobs.telemetry_writer, "enable_agentless_client", lambda *a, **k: None)
    monkeypatch.setattr(ddtrace.config, "_dd_api_key", ddtrace.config._dd_api_key)
    monkeypatch.setattr(ddtrace.config, "_llmobs_agentless_enabled", None)
    monkeypatch.delenv("DD_APM_TRACING_ENABLED", raising=False)

    assert not LLMObs.enabled
    LLMObs.enable(
        agent_service="cbioportal-mcp-test",
        api_key="test-not-a-real-key",
        site="datadoghq.com",
        agentless_enabled=True,
        integrations_enabled=False,
    )
    assert LLMObs._instance._export_mode == "apm_agentless"
    spans: list = []
    writer = LLMObs._instance.tracer._span_aggregator.writer
    monkeypatch.setattr(writer, "write", lambda trace: spans.extend(trace or []))
    try:
        yield spans
    finally:
        LLMObs.disable()
        assert attempted == [], f"unexpected network connections: {attempted}"


def _make_app():
    mcp = FastMCP("llmobs-test", middleware=[TelemetryMiddleware()])

    @mcp.tool()
    def ping(word: str = "pong") -> str:
        return word

    @mcp.tool()
    def boom() -> str:
        raise ValueError("kaboom")

    return mcp.http_app(path="/mcp", stateless_http=False)


def _call_tool(client: TestClient, tool: str, arguments: dict, headers: dict | None = None):
    merged = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "9.9.9"},
            },
        },
        headers=merged,
    )
    assert init.status_code == 200, init.text
    session_id = init.headers["mcp-session-id"]
    merged["mcp-session-id"] = session_id
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=merged,
    )
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=merged,
    )
    assert resp.status_code == 200, resp.text
    return session_id, resp


def _tags(event: dict) -> dict[str, str]:
    return dict(tag.split(":", 1) for tag in event["tags"])


def _tool_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["meta"]["span"]["kind"] == "tool"]


def test_tool_call_emits_one_llmobs_tool_span(llmobs_events):
    with TestClient(_make_app()) as client:
        session_id, resp = _call_tool(
            client,
            "ping",
            {"word": "hello"},
            headers={"x-user-id": "507f1f77bcf86cd799439011", "x-user-email": "a@b.org"},
        )
    assert "hello" in resp.text

    tool_events = _tool_events(llmobs_events)
    assert len(tool_events) == 1, llmobs_events
    event = tool_events[0]

    assert event["name"] == "mcp.tool.ping"
    assert event["status"] == "ok"
    assert event["session_id"] == session_id

    tags = _tags(event)
    assert tags["ml_app"] == "cbioportal-mcp-test"
    assert tags["usr_id"] == "507f1f77bcf86cd799439011"
    assert tags["mcp_client_kind"] == "librechat"
    assert tags["mcp_client_name"] == "claude-code"
    assert tags["mcp_session_id"] == session_id
    assert tags["error"] == "0"

    meta = event["meta"]
    assert json.loads(meta["input"]["value"]) == {"word": "hello"}
    assert "hello" in meta["output"]["value"]
    assert meta["metadata"]["user_email"] == "a@b.org"
    assert meta["metadata"]["mcp_client_name"] == "claude-code"
    assert meta["metadata"]["mcp_client_version"] == "9.9.9"
    assert meta["metadata"]["mcp_session_id"] == session_id


def test_erroring_tool_emits_llmobs_span_with_error(llmobs_events):
    with TestClient(_make_app()) as client:
        _call_tool(client, "boom", {})

    tool_events = _tool_events(llmobs_events)
    assert len(tool_events) == 1, llmobs_events
    event = tool_events[0]

    assert event["name"] == "mcp.tool.boom"
    assert event["status"] == "error"
    assert _tags(event)["error"] == "1"
    # FastMCP wraps the tool's exception in a ToolError before middleware sees it.
    assert event["meta"]["error"]["type"] == "fastmcp.exceptions.ToolError"
    assert "kaboom" in event["meta"]["error"]["message"]
    assert event["meta"]["output"]["value"] == "error"


def test_each_tool_call_gets_its_own_span(llmobs_events):
    with TestClient(_make_app()) as client:
        _call_tool(client, "ping", {"word": "one"})
        _call_tool(client, "ping", {"word": "two"})

    tool_events = _tool_events(llmobs_events)
    assert [json.loads(e["meta"]["input"]["value"])["word"] for e in tool_events] == [
        "one",
        "two",
    ]
    assert tool_events[0]["span_id"] != tool_events[1]["span_id"]
    # Separate calls are separate traces, not nested under a leaked active span.
    assert all(e["parent_id"] == "undefined" for e in tool_events)


def test_noop_when_llmobs_not_enabled(caplog):
    """Without DD_API_KEY, server.py never calls LLMObs.enable(): no span is started,
    nothing is logged, and the tool call works normally."""
    assert not LLMObs.enabled
    with (
        patch.object(LLMObs, "tool", wraps=LLMObs.tool) as tool_spy,
        caplog.at_level(logging.WARNING),
        TestClient(_make_app()) as client,
    ):
        _, resp = _call_tool(client, "ping", {"word": "hello"})

    assert "hello" in resp.text
    tool_spy.assert_not_called()
    assert not [r for r in caplog.records if r.name.startswith(("cbioportal_mcp", "ddtrace"))]


def test_span_creation_failure_warns_once_and_never_breaks_tool(llmobs_events, caplog):
    with (
        patch.object(LLMObs, "tool", side_effect=RuntimeError("sdk broke")),
        caplog.at_level(logging.WARNING, logger="cbioportal_mcp.telemetry"),
        TestClient(_make_app()) as client,
    ):
        _, first = _call_tool(client, "ping", {"word": "first"})
        _, second = _call_tool(client, "ping", {"word": "second"})

    assert "first" in first.text and "second" in second.text
    warnings = [
        r
        for r in caplog.records
        if r.name == "cbioportal_mcp.telemetry" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "LLMObs" in warnings[0].getMessage()
    assert _tool_events(llmobs_events) == []


def test_prod_mode_llmobs_tags_are_underscored_and_apm_tags_dotted(prod_mode_spans):
    """In the production export mode ddtrace rewrites dots in LLMObs tag keys to
    underscores; we send underscore keys so the names are the same in every mode.
    The APM span keeps the dotted tags."""
    with TestClient(_make_app()) as client:
        session_id, resp = _call_tool(
            client, "ping", {"word": "hello"}, headers={"x-user-id": "u-123"}
        )
    assert "hello" in resp.text

    tool_spans = [s for s in prod_mode_spans if s.name == "mcp.tool.ping"]
    assert len(tool_spans) == 1, [s.name for s in prod_mode_spans]
    span = tool_spans[0]

    payload = span._get_struct_tag("_llmobs")
    assert payload is not None, "LLMObs payload did not ride the APM trace"
    llmobs_tags = payload["tags"]
    assert llmobs_tags["usr_id"] == "u-123"
    assert llmobs_tags["mcp_client_kind"] == "librechat"
    assert llmobs_tags["mcp_client_name"] == "claude-code"
    assert llmobs_tags["mcp_session_id"] == session_id
    assert not any(k.startswith(("usr.", "mcp.")) for k in llmobs_tags), llmobs_tags

    assert span.get_tag("usr.id") == "u-123"
    assert span.get_tag("mcp.client_kind") == "librechat"
    assert span.get_tag("mcp.client.name") == "claude-code"
    assert span.get_tag("mcp.session.id") == session_id


# --- Direct middleware tests (no HTTP): concurrency, threads, cancellation ----------


def _ctx(tool: str) -> MiddlewareContext:
    return MiddlewareContext(message=mt.CallToolRequestParams(name=tool, arguments={}))


def _ok_result(text: str) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)])


def test_overlapping_calls_get_distinct_root_spans(llmobs_events):
    middleware = TelemetryMiddleware()

    async def main():
        both_started = asyncio.Event()
        arrivals = 0

        async def call_next(context):
            # Neither call returns until both are in flight, so the spans overlap.
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_started.set()
            await both_started.wait()
            return _ok_result(context.message.name)

        return await asyncio.gather(
            middleware.on_call_tool(_ctx("a"), call_next),
            middleware.on_call_tool(_ctx("b"), call_next),
        )

    asyncio.run(main())

    events = sorted(_tool_events(llmobs_events), key=lambda e: e["name"])
    assert [e["name"] for e in events] == ["mcp.tool.a", "mcp.tool.b"]
    assert events[0]["span_id"] != events[1]["span_id"]
    assert events[0]["trace_id"] != events[1]["trace_id"]
    assert all(e["parent_id"] == "undefined" for e in events)
    assert "a" in events[0]["meta"]["output"]["value"]
    assert "b" in events[1]["meta"]["output"]["value"]


@pytest.mark.parametrize("offload", ["asyncio", "anyio"])
def test_worker_thread_child_span_is_parented_to_tool_span(llmobs_events, offload):
    middleware = TelemetryMiddleware()

    def blocking_work():
        with LLMObs.task(name="worker.child"):
            return "done"

    async def call_next(context):
        if offload == "asyncio":
            text = await asyncio.to_thread(blocking_work)
        else:
            text = await anyio.to_thread.run_sync(blocking_work)
        return _ok_result(text)

    asyncio.run(middleware.on_call_tool(_ctx("threaded"), call_next))

    by_name = {e["name"]: e for e in llmobs_events}
    tool, child = by_name["mcp.tool.threaded"], by_name["worker.child"]
    assert child["parent_id"] == tool["span_id"]
    assert child["trace_id"] == tool["trace_id"]
    assert tool["parent_id"] == "undefined"


def test_cancelled_call_finishes_span_with_error(llmobs_events):
    middleware = TelemetryMiddleware()

    async def main():
        entered = asyncio.Event()

        async def call_next(context):
            entered.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(middleware.on_call_tool(_ctx("slow"), call_next))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    (event,) = _tool_events(llmobs_events)
    assert event["name"] == "mcp.tool.slow"
    assert event["status"] == "error"
    assert event["meta"]["error"]["type"].endswith("CancelledError")
