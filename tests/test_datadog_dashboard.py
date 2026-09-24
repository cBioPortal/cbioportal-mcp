import json
from pathlib import Path


def test_tool_metrics_dashboard_json_is_valid_and_references_metrics():
    dashboard = json.loads(
        Path("datadog/cbioagent-tool-metrics-dashboard.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(dashboard)

    assert dashboard["title"] == "cBioAgent MCP Tool Metrics"
    assert dashboard["widgets"][0]["definition"]["title"] == "MCP Tool Metrics"
    assert dashboard["widgets"][0]["definition"]["type"] == "group"
    assert "cbioportal_mcp.tool.calls" in serialized
    assert "cbioportal_mcp.tool.duration_ms" in serialized
    assert "cbioportal_mcp.tool.errors" in serialized
    assert "tool" in serialized
    assert "client_kind" in serialized
