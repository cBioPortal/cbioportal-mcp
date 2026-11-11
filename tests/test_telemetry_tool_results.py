"""Returned tool errors must fail telemetry while preserving the MCP result."""

from types import SimpleNamespace
from unittest.mock import Mock

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools.tool import ToolResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from cbioportal_mcp import telemetry


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"error_message": "permission denied"}, "permission denied"),
        (ToolResult(structured_content={"error_message": "SQL failed"}), "SQL failed"),
        (mt.CallToolResult(content=[], structuredContent={"error_message": "DB down"}), "DB down"),
        (ToolResult(content={"error_message": "text error"}), "text error"),
        (
            mt.CallToolResult(
                content=[mt.TextContent(type="text", text='{"error_message":"bad"}')]
            ),
            "bad",
        ),
        ({"error_message": "x" * 2000}, "x" * 1000),
        ({"error_message": ""}, None),
        ({"error_message": None}, None),
        ({"rows": [{"error_message": "data value"}]}, None),
        (ToolResult(content="not JSON"), None),
        (ToolResult(content='[ {"error_message": "nested"} ]'), None),
        (ToolResult(structured_content={"rows": []}), None),
        ("pong", None),
    ],
)
def test_tool_result_error_shapes(result, expected):
    assert telemetry._tool_result_error(result) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["result", "success", "exception"])
async def test_tool_outcome_spans_metrics_and_return(monkeypatch, outcome):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda _: provider.get_tracer("test"))
    metrics = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: metrics)
    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    context = SimpleNamespace(
        message=SimpleNamespace(name="query", arguments={}), fastmcp_context=None
    )
    result = {"error_message": "permission denied"} if outcome == "result" else {"rows": []}
    exception = RuntimeError("connection failed")

    async def call_next(_):
        if outcome == "exception":
            raise exception
        return result

    middleware = telemetry.TelemetryMiddleware()
    if outcome == "exception":
        with pytest.raises(RuntimeError) as caught:
            await middleware.on_call_tool(context, call_next)
        assert caught.value is exception
    else:
        assert await middleware.on_call_tool(context, call_next) is result

    (span,) = exporter.get_finished_spans()
    success = outcome == "success"
    assert span.attributes["mcp.tool.success"] is success
    tags = metrics.increment.call_args_list[0].args[1]
    assert tags["success"] == str(success).lower()
    if success:
        assert "error_kind" not in tags
        assert "error.type" not in span.attributes
        assert span.status.status_code == StatusCode.UNSET
        assert [c.args[0] for c in metrics.increment.call_args_list] == ["tool.calls"]
    else:
        assert tags["error_kind"] == outcome
        assert span.attributes["error.type"] == (
            "tool_error_result" if outcome == "result" else "RuntimeError"
        )
        assert span.status.status_code == StatusCode.ERROR
        assert [c.args[0] for c in metrics.increment.call_args_list] == [
            "tool.calls",
            "tool.errors",
        ]
        assert metrics.increment.call_args_list[1].args[1] == tags
        assert "permission denied" not in str(tags)
        assert "connection failed" not in str(tags)
    assert metrics.distribution.call_args.args[0] == "tool.duration_ms"
    assert metrics.distribution.call_args.args[2] == tags
    provider.shutdown()


@pytest.mark.asyncio
async def test_fastmcp_middleware_receives_structured_error_result(monkeypatch):
    """Exercise the actual FunctionTool -> middleware result conversion."""
    metrics = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: metrics)
    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    server = FastMCP("error-result", middleware=[telemetry.TelemetryMiddleware()])
    payload = {"error_message": "SQL permission denied"}

    @server.tool()
    def query() -> dict:
        return payload

    async with Context(server):
        result = await server._call_tool_middleware("query", {})
    assert isinstance(result, ToolResult)
    assert result.structured_content == payload
    assert result.content[0].text == '{"error_message":"SQL permission denied"}'
    assert metrics.increment.call_args_list[0].args[1]["success"] == "false"
    assert metrics.increment.call_args_list[1].args[0] == "tool.errors"
    assert metrics.increment.call_args_list[1].args[1]["error_kind"] == "result"
