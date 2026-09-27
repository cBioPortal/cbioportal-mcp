"""Exercise the SELECT funnel and the pinned upstream worker without a database."""

from concurrent.futures import TimeoutError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp_clickhouse import mcp_server as upstream

from cbioportal_mcp import query_cache, server, telemetry


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", "CBIOPORTAL_MCP_QUERY_CACHE_TTL"):
        monkeypatch.delenv(name, raising=False)
    config = Mock()
    config.get_client_config.side_effect = lambda: {"host": "test", "username": "reader"}
    config.allow_write_access = False
    monkeypatch.setattr(upstream, "get_config", lambda: config)


@pytest.fixture
def database(monkeypatch):
    client = Mock()
    client.query.return_value = SimpleNamespace(column_names=["n"], result_rows=[[42]])
    entry = upstream._ClientCacheEntry(client=client, last_used=0, active_users=1)
    acquire = Mock(return_value=entry)
    monkeypatch.setattr(upstream, "_acquire_clickhouse_client", acquire)
    monkeypatch.setattr(upstream, "_release_client_entry", Mock())
    monkeypatch.setattr(upstream, "get_readonly_setting", lambda client: "1")
    return client, acquire


@pytest.mark.parametrize("flag", [None, "0", "true", "1"])
@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM gene_mutation_frequency_by_cancer_type(preference='public', gene='TP53')",
        "SELECT * FROM top_mutated_genes_in_cohort(preference='public')",
        "WITH x AS (SELECT 42 AS n) SELECT n FROM x",
    ],
)
def test_funnel_settings_and_metrics(monkeypatch, database, flag, query):
    if flag is not None:
        monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", flag)
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_TTL", "120")
    metrics = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: metrics)
    client, acquire = database

    assert server.run_select_query(query, query_label="clickhouse_run_select_query") == [{"n": 42}]
    config = acquire.call_args.args[0]
    if flag == "1":
        assert config["settings"] == {
            "use_query_cache": 1,
            "query_cache_ttl": 120,
            "query_cache_nondeterministic_function_handling": "ignore",
        }
    else:
        assert "use_query_cache" not in config.get("settings", {})
    assert client.query.call_args.args == (query,)
    assert client.query.call_args.kwargs["settings"]["readonly"] == "1"
    assert "query_id" in client.query.call_args.kwargs["settings"]
    tags = {"query_label": "clickhouse_run_select_query", "success": "true"}
    if flag == "1":
        tags["query_cache"] = "on"
    metrics.increment.assert_called_once_with("db_query.calls", tags)
    assert metrics.distribution.call_args.args[0] == "db_query.duration_ms"
    assert metrics.distribution.call_args.args[2] == tags


def test_disabled_delegates_byte_identical_without_settings(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_TTL", "invalid")
    payload = '{"columns": ["n"], "rows": [[42]]}'
    delegate = Mock(return_value=payload)
    monkeypatch.setattr(upstream, "run_query", delegate)
    query = " SELECT 42 AS n; -- unchanged\n"
    assert query_cache.run_query(query, settings=query_cache.query_cache_settings()) is payload
    delegate.assert_called_once_with(query)


def test_defaults(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", "1")
    assert query_cache.query_cache_settings() == {
        "use_query_cache": 1,
        "query_cache_ttl": 3600,
        "query_cache_nondeterministic_function_handling": "ignore",
    }


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_invalid_settings(monkeypatch, value):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", "1")
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_TTL", value)
    with pytest.raises(ValueError):
        query_cache.query_cache_settings()


def test_request_credentials_and_settings_preserved(monkeypatch, database):
    overrides = {"username": "alice", "settings": {"role": "reader", "max_threads": 2}}
    monkeypatch.setattr(upstream, "_get_client_config_overrides", lambda: overrides)
    query_cache.run_query("SELECT 1", settings={"use_query_cache": 1})
    config = database[1].call_args.args[0]
    assert config["username"] == "alice"
    assert config["settings"] == {"role": "reader", "max_threads": 2, "use_query_cache": 1}
    assert overrides == {"username": "alice", "settings": {"role": "reader", "max_threads": 2}}


def test_query_failure_records_cache_tag(monkeypatch, database):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", "1")
    database[0].query.side_effect = ValueError("denied")
    metrics = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: metrics)
    result = server.clickhouse_run_select_query.fn("SELECT 1")
    assert "denied" in result["error_message"]
    metrics.increment.assert_any_call(
        "db_query.errors",
        {"query_label": "clickhouse_run_select_query", "success": "false", "query_cache": "on"},
    )


@pytest.mark.parametrize("queued", [True, False])
def test_cache_path_preserves_timeout_and_cancellation(monkeypatch, queued):
    future = Mock()
    future.result.side_effect = TimeoutError()
    future.cancel.return_value = queued
    executor = Mock()
    executor.submit.return_value = future
    cancel = Mock()
    monkeypatch.setattr(upstream, "QUERY_EXECUTOR", executor)
    monkeypatch.setattr(upstream, "_cancel_query_with_bounded_wait", cancel)
    with pytest.raises(upstream.ToolError, match="Query timed out"):
        query_cache.run_query("SELECT 1", settings={"use_query_cache": 1})
    query_id = executor.submit.call_args.args[2]
    future.result.assert_called_once_with(timeout=upstream.get_mcp_config().query_timeout)
    future.cancel.assert_called_once_with()
    if queued:
        cancel.assert_not_called()
        assert query_id not in upstream._active_queries
    else:
        cancel.assert_called_once_with(query_id)
        assert upstream._active_queries[query_id].cancelled
        upstream._remove_active_query(query_id, upstream._active_queries[query_id])


def test_submit_failure_cleans_up(monkeypatch):
    executor = Mock()
    executor.submit.side_effect = RuntimeError("executor closed")
    monkeypatch.setattr(upstream, "QUERY_EXECUTOR", executor)
    with pytest.raises(RuntimeError, match="executor closed"):
        query_cache.run_query("SELECT 1", settings={"use_query_cache": 1})
    assert executor.submit.call_args.args[2] not in upstream._active_queries


def test_enabled_preserves_row_cap(monkeypatch, database):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED", "1")
    server.run_select_query("SELECT 42 AS n;", query_label="test", max_rows=10)
    assert database[0].query.call_args.args == ("SELECT * FROM (SELECT 42 AS n) LIMIT 11",)
