"""Query latency attribution: query_concurrency.run_query + traced_db_query.

run_query() is mcp-clickhouse 0.5.0's run_query()/execute_query() plus
measurement. These tests run both against the same fake ClickHouse client and
pool and check that results, settings, errors, timeouts and KILL QUERY are
identical, and that the new span tags / metrics are recorded.
"""

import asyncio
import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import mcp_clickhouse.mcp_server as mc
import pytest
from fastmcp.exceptions import ToolError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cbioportal_mcp import query_concurrency, server, telemetry

SUMMARY = {
    "read_rows": "1200",
    "read_bytes": "34567",
    "written_rows": "0",
    "written_bytes": "0",
    "total_rows_to_read": "5000",
    "result_rows": "2",
    "result_bytes": "64",
    "elapsed_ns": "12500000",
    "query_id": "ignored-here",
}


class FakeClient:
    """clickhouse-connect stand-in. Records each query's settings."""

    server_settings = {}

    def __init__(self, *, delay=0.0, hang=False, error=None, summary=SUMMARY):
        self.delay = delay
        self.hang = hang
        self.error = error
        self.summary = summary
        self.release = threading.Event()
        self.calls = []
        self.kills = []
        self.lock = threading.Lock()

    def query(self, query, settings=None):
        with self.lock:
            self.calls.append((query, dict(settings or {})))
        if self.hang:
            self.release.wait(10)
        elif self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        res = SimpleNamespace(column_names=["n", "big"], result_rows=[[1, 2**60], [2, None]])
        if self.summary is not None:
            res.summary = dict(self.summary)
        return res

    def command(self, sql):
        with self.lock:
            self.kills.append(sql)


@pytest.fixture
def ch(monkeypatch):
    """Install a fake client and a fresh query pool of a given size on mc."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "fake-clickhouse")
    monkeypatch.setenv("CLICKHOUSE_USER", "fake")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "fake")
    monkeypatch.setenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "1")
    installed = []

    def install(client, workers=4):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        installed.append((pool, client))
        monkeypatch.setattr(mc, "QUERY_EXECUTOR", pool)
        monkeypatch.setattr(
            mc,
            "_acquire_clickhouse_client",
            lambda _config: mc._ClientCacheEntry(
                client=client, last_used=time.time(), active_users=1
            ),
        )
        return client

    yield install
    for pool, client in installed:
        client.release.set()
        pool.shutdown(wait=True)


def _outcome(fn, query):
    try:
        return ("ok", fn(query))
    except BaseException as exc:  # compare the exact exception type and message
        return (type(exc), str(exc))


# --- preserved semantics -----------------------------------------------------------


def test_result_and_settings_match_mcp_clickhouse(ch):
    client = ch(FakeClient())
    ours = query_concurrency.run_query("SELECT 1")
    theirs = mc.run_query("SELECT 1")

    # Same serialized payload (incl. mcp-clickhouse's unsafe-integer handling).
    assert ours == theirs
    assert json.loads(ours)["rows"][0][1] == str(2**60)
    # Same settings sent: readonly + query_id only, no new ClickHouse SETTINGS.
    (_, our_settings), (_, their_settings) = client.calls
    assert set(our_settings) == set(their_settings) == {"readonly", "query_id"}
    assert our_settings["readonly"] == their_settings["readonly"] == "1"
    assert not mc._active_queries


@pytest.mark.parametrize(
    "error",
    [RuntimeError("Code: 47. Unknown identifier"), ConnectionError("connection reset by peer")],
)
def test_execution_errors_match_mcp_clickhouse(ch, monkeypatch, error):
    ch(FakeClient(error=error))
    evicted = []
    monkeypatch.setattr(mc, "_evict_cached_client", lambda cfg, c: evicted.append(c))

    ours = _outcome(query_concurrency.run_query, "SELECT bad")
    theirs = _outcome(mc.run_query, "SELECT bad")

    assert ours == theirs == (ToolError, f"Query execution failed: {error}")
    # Connection errors evict the cached client in both; query errors in neither.
    assert len(evicted) == (2 if isinstance(error, ConnectionError) else 0)
    assert not mc._active_queries


def test_unexpected_error_matches_mcp_clickhouse(ch, monkeypatch):
    ch(FakeClient())
    broken = Mock()
    broken.submit.side_effect = OSError("pool is shut down")
    monkeypatch.setattr(mc, "QUERY_EXECUTOR", broken)

    ours = _outcome(query_concurrency.run_query, "SELECT 1")
    theirs = _outcome(mc.run_query, "SELECT 1")

    assert (
        ours
        == theirs
        == (
            RuntimeError,
            "Unexpected error during query execution: pool is shut down",
        )
    )
    assert not mc._active_queries


def test_running_query_timeout_kills_it_with_the_recorded_query_id(ch):
    client = ch(FakeClient(hang=True))
    stats = query_concurrency.QueryStats()

    with query_concurrency.recording_query_stats(stats):
        ours = _outcome(query_concurrency.run_query, "SELECT sleep(10)")

    assert ours == (ToolError, "Query timed out after 1 seconds")
    assert stats.timed_out is True
    # KILL QUERY targets the same query_id we record for system.query_log.
    (kill,) = client.kills
    assert kill == f"KILL QUERY WHERE query_id = '{stats.query_id}'"
    assert client.calls[0][1]["query_id"] == stats.query_id
    assert stats.queue_wait_ms is not None and stats.execute_ms is None

    # mcp-clickhouse's own run_query on the same (4-worker) pool behaves identically.
    theirs = _outcome(mc.run_query, "SELECT sleep(10)")
    assert theirs == ours
    assert len(client.kills) == 2


def test_query_still_queued_at_timeout_is_cancelled_without_kill(ch):
    """Queue wait counts against the 30s (here 1s) timeout, as in mcp-clickhouse."""
    client = ch(FakeClient(hang=True), workers=1)
    blocker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    blocker.submit(_outcome, query_concurrency.run_query, "SELECT blocker")
    while not client.calls:
        time.sleep(0.01)
    stats = query_concurrency.QueryStats()

    with query_concurrency.recording_query_stats(stats):
        queued = _outcome(query_concurrency.run_query, "SELECT queued")

    assert queued == (ToolError, "Query timed out after 1 seconds")
    assert stats.timed_out and stats.started_at is None and stats.queue_wait_ms is None
    # Only the blocker ever reached ClickHouse; the queued one was cancelled, not killed.
    assert [q for q, _ in client.calls] == ["SELECT blocker"]
    _, attrs = telemetry._db_query_breakdown(stats, time.perf_counter())
    assert attrs["db.query.never_started"] is True
    assert attrs["db.query.queue_wait_ms"] >= 950
    client.release.set()
    blocker.shutdown(wait=True)


# --- recorded facts ----------------------------------------------------------------


def test_stats_record_query_id_summary_and_timings(ch):
    client = ch(FakeClient(delay=0.05))
    stats = query_concurrency.QueryStats()

    with query_concurrency.recording_query_stats(stats):
        payload = query_concurrency.run_query("SELECT 1")

    assert stats.query_id == client.calls[0][1]["query_id"]
    assert stats.server == {
        "elapsed_ns": 12500000,
        "read_rows": 1200,
        "read_bytes": 34567,
        "result_rows": 2,
        "result_bytes": 64,
        "total_rows_to_read": 5000,
    }
    assert stats.execute_ms >= 45
    assert stats.queue_wait_ms >= 0 and stats.client_acquire_ms >= 0
    assert stats.rows_returned == 2 and stats.payload_chars == len(payload)
    assert stats.returned_at is not None and not stats.timed_out


def test_missing_or_partial_summary_is_tolerated(ch):
    ch(FakeClient(summary={"read_rows": "7", "elapsed_ns": "not-a-number"}))
    stats = query_concurrency.QueryStats()
    with query_concurrency.recording_query_stats(stats):
        query_concurrency.run_query("SELECT 1")
    assert stats.server == {"read_rows": 7}

    ch(FakeClient(summary=None))
    stats = query_concurrency.QueryStats()
    with query_concurrency.recording_query_stats(stats):
        query_concurrency.run_query("SELECT 1")
    assert stats.server == {}


def test_queue_wait_is_measured_when_the_pool_is_saturated(ch):
    ch(FakeClient(delay=0.3), workers=1)
    stats = [query_concurrency.QueryStats() for _ in range(2)]

    def run(s):
        with query_concurrency.recording_query_stats(s):
            query_concurrency.run_query("SELECT 1")

    threads = [threading.Thread(target=run, args=(s,)) for s in stats]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    waits = sorted(s.queue_wait_ms for s in stats)
    assert waits[0] < 100  # first query got the worker immediately
    assert waits[1] >= 250  # second waited for the first to finish (~300ms)
    assert all(s.execute_ms >= 250 for s in stats)  # execution excludes the wait


def test_verify_query_runner(monkeypatch):
    query_concurrency.verify_query_runner()  # the pinned mcp-clickhouse 0.5.0
    monkeypatch.delattr(mc, "_cancel_query_with_bounded_wait")
    with pytest.raises(RuntimeError, match="_cancel_query_with_bounded_wait"):
        query_concurrency.verify_query_runner()


# --- telemetry -------------------------------------------------------------------


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda _: provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


@pytest.fixture
def metrics(monkeypatch):
    client = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: client)
    return client


def _distributions(metrics):
    return {c.args[0]: (c.args[1], c.args[2]) for c in metrics.distribution.call_args_list}


def test_run_select_query_tags_span_and_metrics(ch, spans, metrics):
    client = ch(FakeClient())

    rows = server.run_select_query("SELECT 1", query_label="study_guide.counts", max_rows=5)

    assert rows == [{"n": 1, "big": str(2**60)}, {"n": 2}]
    (span,) = spans.get_finished_spans()
    attrs = span.attributes
    assert span.name == "db.query/study_guide.counts"
    assert attrs["db.clickhouse.query_id"] == client.calls[0][1]["query_id"]
    assert attrs["db.clickhouse.elapsed_ms"] == pytest.approx(12.5)
    assert attrs["db.clickhouse.read_rows"] == 1200
    assert attrs["db.clickhouse.read_bytes"] == 34567
    assert attrs["db.clickhouse.result_rows"] == 2
    assert attrs["db.clickhouse.result_bytes"] == 64
    assert attrs["db.clickhouse.total_rows_to_read"] == 5000
    for key in (
        "db.query.queue_wait_ms",
        "db.query.client_acquire_ms",
        "db.query.execute_ms",
        "db.query.serialize_ms",
        "db.query.postprocess_ms",
        "db.query.payload_chars",
        "db.query.duration_ms",
    ):
        assert attrs[key] >= 0, key
    assert attrs["db.query.rows_returned"] == 2
    assert attrs["db.query.timed_out"] is False
    assert attrs["db.query.success"] is True
    # The parts fit inside the whole.
    assert (
        attrs["db.query.queue_wait_ms"] + attrs["db.query.execute_ms"]
        <= attrs["db.query.duration_ms"]
    )

    tags = {"query_label": "study_guide.counts", "success": "true"}
    dists = _distributions(metrics)
    assert set(dists) == {
        "db_query.duration_ms",
        "db_query.queue_wait_ms",
        "db_query.execute_ms",
        "db_query.payload_chars",
        "db_query.ch_elapsed_ms",
        "db_query.ch_read_rows",
        "db_query.ch_read_bytes",
        "db_query.ch_result_rows",
        "db_query.ch_result_bytes",
    }
    assert dists["db_query.ch_elapsed_ms"][0] == pytest.approx(12.5)
    assert dists["db_query.ch_read_rows"][0] == 1200
    # Low-cardinality tags only: never the query_id.
    for _, metric_tags in dists.values():
        assert metric_tags == tags
    assert attrs["db.clickhouse.query_id"] not in str(metrics.mock_calls)


def test_timed_out_query_is_tagged_and_error_shape_unchanged(ch, spans, metrics):
    client = ch(FakeClient(hang=True))

    result = server.clickhouse_run_select_query.fn.__wrapped__("SELECT sleep(10)")

    assert result == {"error_message": "Query timed out after 1 seconds"}
    (span,) = spans.get_finished_spans()
    assert span.attributes["db.query.timed_out"] is True
    assert span.attributes["db.query.success"] is False
    assert span.attributes["error.type"] == "ToolError"
    assert client.kills == [
        f"KILL QUERY WHERE query_id = '{span.attributes['db.clickhouse.query_id']}'"
    ]
    tags = {"query_label": "clickhouse_run_select_query", "success": "false"}
    assert _distributions(metrics)["db_query.queue_wait_ms"][1] == tags
    assert "db_query.ch_elapsed_ms" not in _distributions(metrics)


def test_schema_tools_go_through_the_observed_runner(ch, spans, metrics):
    server._clear_schema_cache()
    ch(FakeClient())
    try:
        server.clickhouse_list_tables.fn.__wrapped__()
        server.clickhouse_list_table_columns.fn.__wrapped__("sample")
    finally:
        server._clear_schema_cache()
    names = [s.name for s in spans.get_finished_spans()]
    assert names == ["db.query/schema.list_tables", "db.query/schema.describe_table"]
    assert all("db.clickhouse.query_id" in s.attributes for s in spans.get_finished_spans())


def test_traced_db_query_without_a_query_adds_nothing(spans, metrics):
    with telemetry.traced_db_query("noop"):
        pass
    (span,) = spans.get_finished_spans()
    assert "db.clickhouse.query_id" not in span.attributes
    assert set(_distributions(metrics)) == {"db_query.duration_ms"}


def test_breakdown_failure_never_fails_the_query(spans, metrics, monkeypatch):
    monkeypatch.setattr(telemetry, "_db_query_breakdown", Mock(side_effect=ValueError("bad stats")))
    with telemetry.traced_db_query("x") as stats:
        stats.query_id = "abc"
    assert set(_distributions(metrics)) == {"db_query.duration_ms"}


def test_tool_metrics_include_result_size_and_total_duration(spans, metrics, monkeypatch):
    import mcp.types as mt

    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)

    def slow_finish(*args, **kwargs):
        time.sleep(0.05)  # stands in for LLMObs output annotation/serialization

    monkeypatch.setattr(telemetry, "_llmobs_finish", slow_finish)
    result = SimpleNamespace(content=[mt.TextContent(type="text", text="x" * 123)])

    async def call_next(_):
        return result

    context = SimpleNamespace(
        message=SimpleNamespace(name="clickhouse_run_select_query", arguments={}),
        fastmcp_context=None,
    )
    asyncio.run(telemetry.TelemetryMiddleware().on_call_tool(context, call_next))

    dists = _distributions(metrics)
    assert dists["tool.result_chars"][0] == 123
    assert dists["tool.total_duration_ms"][1] == dists["tool.duration_ms"][1]
    # tool.duration_ms keeps its meaning; the total includes the post-processing.
    assert dists["tool.total_duration_ms"][0] >= dists["tool.duration_ms"][0] + 45
    (span,) = spans.get_finished_spans()
    assert span.attributes["mcp.tool.result_chars"] == 123
