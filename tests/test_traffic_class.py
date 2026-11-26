"""Benchmark traffic is tagged mcp.traffic_class=benchmark so usage counts can exclude it."""

import pytest

from cbioportal_mcp.telemetry import traffic_class


def test_user_traffic_by_default():
    assert traffic_class("u123", "a@example.org", "claude-code") == "user"
    assert traffic_class(None, None, None) == "user"


def test_benchmark_by_client_name():
    assert traffic_class(None, None, "cbioportal-mcp-qa") == "benchmark"


def test_benchmark_by_user_id():
    assert traffic_class("cbioportal-mcp-qa", "cbioportal-mcp-qa@localhost", None) == "benchmark"


def test_benchmark_ids_from_env(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_BENCHMARK_IDS", " 64f0abc , qa-bot@example.org ")
    assert traffic_class("64f0abc", None, None) == "benchmark"
    assert traffic_class("other", "qa-bot@example.org", None) == "benchmark"
    assert traffic_class("other", "someone@example.org", None) == "user"
