"""
TelemetryMiddleware → Datadog LLM Observability tool spans, through the REAL ddtrace
LLMObs SDK (not a stub of ``_llmobs_tool_span``).

LLMObs is enabled in agent-proxy mode with APM tracing disabled, so every finished
LLMObs span is handed to the LLMObs span writer as a fully-assembled span event.
The writer's ``enqueue`` is replaced with a list append, so events are captured
in-process and nothing is sent anywhere — no DD_API_KEY and no Datadog network
calls are involved.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from ddtrace.llmobs import LLMObs
from fastmcp import FastMCP
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
    assert tags["usr.id"] == "507f1f77bcf86cd799439011"
    assert tags["mcp.client_kind"] == "librechat"
    assert tags["mcp.client.name"] == "claude-code"
    assert tags["mcp.session.id"] == session_id
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
