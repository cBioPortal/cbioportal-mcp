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

    def __init__(self, *, delay=0.0, hang=False, error=None, summary=SUMMARY, kill_blocks=False):
        self.delay = delay
        self.hang = hang
        self.error = error
        self.summary = summary
        self.kill_blocks = kill_blocks
        self.release = threading.Event()
        self.entered = threading.Event()  # set once the first query reaches ClickHouse
        self.calls = []
        self.kills = []
        self.lock = threading.Lock()

    def query(self, query, settings=None):
        with self.lock:
            self.calls.append((query, dict(settings or {})))
        self.entered.set()
        if self.hang:
            self.release.wait(10)
        elif self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        res = SimpleNamespace(column_names=["n", "big"], result_rows=[[1, 2**60], [2, None]])
        if self.summary is not None:
            res.summary = dict(self.summary) if type(self.summary) is dict else self.summary
        return res

    def command(self, sql):
        with self.lock:
            self.kills.append(sql)
        if self.kill_blocks:
            # KILL QUERY hangs (e.g. ClickHouse unresponsive): run_query must
            # still return after its bounded 1s cancellation wait.
            self.release.wait(10)


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


def _run_with_pool_blocked(client, run, query):
    """Run ``run(query)`` while the 1-worker pool is busy with a hung query."""
    blocker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        blocker.submit(_outcome, query_concurrency.run_query, "SELECT blocker")
        assert client.entered.wait(5), "blocker never reached the fake ClickHouse"
        return run(query)
    finally:
        client.release.set()
        blocker.shutdown(wait=True)


def test_query_still_queued_at_timeout_is_cancelled_without_kill(ch):
    """Queue wait counts against the 30s (here 1s) timeout, as in mcp-clickhouse."""
    client = ch(FakeClient(hang=True), workers=1)
    stats = query_concurrency.QueryStats()

    def run(query):
        with query_concurrency.recording_query_stats(stats):
            return _outcome(query_concurrency.run_query, query)

    queued = _run_with_pool_blocked(client, run, "SELECT queued")
    finished_at = time.perf_counter()

    assert queued == (ToolError, "Query timed out after 1 seconds")
    assert stats.timed_out and stats.started_at is None and stats.queue_wait_ms is None
    assert stats.cancelled_in_queue_at is not None
    # Only the blocker ever reached ClickHouse; the queued one was cancelled, not
    # killed (the blocker itself times out too, so it may have a KILL of its own).
    assert [q for q, _ in client.calls] == ["SELECT blocker"]
    assert not any(stats.query_id in kill for kill in client.kills)
    metrics, attrs = telemetry._db_query_breakdown(stats, finished_at + 60)
    assert attrs["db.query.never_started"] is True
    # Ends when the future was cancelled, not when the span finished (+60s here).
    wait_ms = (stats.cancelled_in_queue_at - stats.submitted_at) * 1000
    assert metrics["queue_wait_ms"] == attrs["db.query.queue_wait_ms"] == wait_ms
    assert 950 <= wait_ms < 5000


def test_queued_cancellation_matches_mcp_clickhouse(ch):
    our_client = ch(FakeClient(hang=True), workers=1)
    ours = _run_with_pool_blocked(
        our_client, lambda q: _outcome(query_concurrency.run_query, q), "SELECT ours"
    )
    their_client = ch(FakeClient(hang=True), workers=1)
    theirs = _run_with_pool_blocked(
        their_client, lambda q: _outcome(mc.run_query, q), "SELECT theirs"
    )

    assert ours == theirs == (ToolError, "Query timed out after 1 seconds")
    # In both, the queued query was cancelled before reaching ClickHouse.
    for client in (our_client, their_client):
        assert [q for q, _ in client.calls] == ["SELECT blocker"]
    assert not mc._active_queries


def test_blocked_kill_is_bounded_like_mcp_clickhouse(ch):
    """A hung KILL QUERY delays the timeout error by at most the 1s bounded wait."""
    client = ch(FakeClient(hang=True, kill_blocks=True))
    results = []
    try:
        for run in (query_concurrency.run_query, mc.run_query):
            started = time.perf_counter()
            outcome = _outcome(run, "SELECT sleep(10)")
            results.append((outcome, time.perf_counter() - started))
    finally:
        client.release.set()

    (ours, our_s), (theirs, their_s) = results
    assert ours == theirs == (ToolError, "Query timed out after 1 seconds")
    # 1s query timeout + 1s bounded cancellation wait, never the 10s KILL hang.
    assert 1.9 <= our_s < 3.5 and 1.9 <= their_s < 3.5
    assert len(client.kills) == 2


def test_rejected_submission_reports_no_queue_wait(ch, monkeypatch, spans, metrics):
    ch(FakeClient())
    broken = Mock()
    broken.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")
    monkeypatch.setattr(mc, "QUERY_EXECUTOR", broken)

    result = server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1")

    assert result == {
        "error_message": (
            "Unexpected error during query execution: " "cannot schedule new futures after shutdown"
        )
    }
    (span,) = spans.get_finished_spans()
    assert "db.query.queue_wait_ms" not in span.attributes
    assert "db.query.never_started" not in span.attributes
    assert set(_distributions(metrics)) == {"db_query.duration_ms"}


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


class _ExplodingSummary(dict):
    def get(self, key, default=None):
        if key == "read_bytes":
            raise RuntimeError("bad header")
        return super().get(key, default)


@pytest.mark.parametrize(
    "summary, expected",
    [
        (
            {
                "elapsed_ns": float("inf"),  # int() -> OverflowError
                "read_rows": float("nan"),  # int() -> ValueError
                "result_rows": object(),  # int() -> TypeError
                "result_bytes": True,  # not a count
                "read_bytes": "12",
            },
            {"read_bytes": 12},
        ),
        (_ExplodingSummary(read_rows="3"), {"read_rows": 3}),
        ("not a mapping", {}),
    ],
)
def test_malformed_summary_never_changes_the_result(ch, summary, expected):
    ch(FakeClient(summary=summary))
    stats = query_concurrency.QueryStats()

    with query_concurrency.recording_query_stats(stats):
        ours = _outcome(query_concurrency.run_query, "SELECT 1")
    theirs = _outcome(mc.run_query, "SELECT 1")

    assert ours[0] == "ok" and ours == theirs
    assert stats.server == expected


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


def test_verify_query_runner_requires_the_vendored_mcp_clickhouse_version(monkeypatch):
    assert query_concurrency._installed_mcp_clickhouse_version() == "0.5.0"
    query_concurrency.verify_query_runner()
    monkeypatch.setattr(query_concurrency, "_installed_mcp_clickhouse_version", lambda: "0.6.0")
    with pytest.raises(RuntimeError, match="0.6.0.*vendored.*query_concurrency.py.*Re-diff"):
        query_concurrency.verify_query_runner()


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
        fastmcp_context=_server_context("clickhouse_run_select_query"),
    )
    asyncio.run(telemetry.TelemetryMiddleware().on_call_tool(context, call_next))

    dists = _distributions(metrics)
    assert dists["tool.result_chars"][0] == 123
    assert dists["tool.result_chars"][1] == dists["tool.total_duration_ms"][1]
    assert dists["tool.total_duration_ms"][1]["tool"] == "clickhouse_run_select_query"
    # tool.duration_ms keeps its meaning; the total includes the post-processing.
    assert dists["tool.total_duration_ms"][0] >= dists["tool.duration_ms"][0] + 45
    (span,) = spans.get_finished_spans()
    assert span.attributes["mcp.tool.result_chars"] == 123


def _caller(client_name):
    return telemetry._CallerContext(
        user_id=None,
        user_email=None,
        client_kind="direct",
        client_name=client_name,
        client_version="9.9",
        session_id=None,
    )


def _server_context(*tool_names, calls=None):
    """A FastMCP Context stand-in whose server has ``tool_names`` registered."""

    async def get_tools():
        if calls is not None:
            calls.append(1)
        return {name: object() for name in tool_names}

    return SimpleNamespace(fastmcp=SimpleNamespace(get_tools=get_tools))


def test_new_tool_metrics_omit_the_unbounded_client_name(spans, metrics, monkeypatch):
    import mcp.types as mt

    arbitrary = "my-homegrown-agent-7f3a9c2e/build-123"
    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    monkeypatch.setattr(telemetry, "_resolve_caller_context", lambda _ctx: _caller(arbitrary))
    result = SimpleNamespace(content=[mt.TextContent(type="text", text="ok")])

    async def call_next(_):
        return result

    context = SimpleNamespace(
        message=SimpleNamespace(name="ping", arguments={}), fastmcp_context=_server_context("ping")
    )
    asyncio.run(telemetry.TelemetryMiddleware().on_call_tool(context, call_next))

    dists = _distributions(metrics)
    bounded = {"tool": "ping", "success": "true", "client_kind": "direct"}
    assert dists["tool.result_chars"][1] == bounded
    assert dists["tool.total_duration_ms"][1] == bounded
    # Existing metrics keep their tags unchanged, client_name included.
    assert dists["tool.duration_ms"][1] == {**bounded, "client_name": arbitrary}
    assert metrics.increment.call_args_list[0].args[1]["client_name"] == arbitrary


def test_cancelled_tool_call_emits_total_duration_and_reraises(spans, metrics, monkeypatch):
    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    finished = []
    monkeypatch.setattr(telemetry, "_llmobs_finish", lambda *a, **kw: finished.append(kw))
    monkeypatch.setattr(telemetry, "_resolve_caller_context", lambda _ctx: _caller("x"))
    entered = asyncio.Event()

    async def call_next(_):
        entered.set()
        await asyncio.sleep(30)

    async def scenario():
        context = SimpleNamespace(
            message=SimpleNamespace(name="slow_tool", arguments={}),
            fastmcp_context=_server_context("slow_tool"),
        )
        task = asyncio.create_task(telemetry.TelemetryMiddleware().on_call_tool(context, call_next))
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # LLMObs cleanup ran with the cancellation as the error, before the total.
    assert isinstance(finished[0]["error"], asyncio.CancelledError)
    dists = _distributions(metrics)
    # tool.duration_ms / tool.calls behaviour is unchanged: nothing for a cancel.
    assert "tool.duration_ms" not in dists
    assert metrics.increment.call_args_list == []
    total_ms, tags = dists["tool.total_duration_ms"]
    assert tags == {
        "tool": "slow_tool",
        "success": "false",
        "client_kind": "direct",
        "error_kind": "cancelled",
    }
    assert total_ms >= 45


# --- bounded `tool` tag on the new metrics -------------------------------------


def _run_tool_call(middleware, name, fastmcp_context, outcome="ok"):
    import mcp.types as mt

    async def call_next(_):
        if outcome == "raise":
            raise ToolError(f"Unknown tool: {name}")
        return SimpleNamespace(content=[mt.TextContent(type="text", text="ok")])

    context = SimpleNamespace(
        message=SimpleNamespace(name=name, arguments={}), fastmcp_context=fastmcp_context
    )
    try:
        asyncio.run(middleware.on_call_tool(context, call_next))
    except ToolError:
        pass


NEW_TOOL_METRICS = ("tool.total_duration_ms", "tool.result_chars")


def _new_metric_tags(metrics):
    return [c.args[2] for c in metrics.distribution.call_args_list if c.args[0] in NEW_TOOL_METRICS]


@pytest.fixture
def quiet_caller(monkeypatch):
    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    monkeypatch.setattr(telemetry, "_resolve_caller_context", lambda _ctx: _caller("x"))


def test_unknown_tool_request_is_tagged_unknown_on_new_metrics(spans, metrics, quiet_caller):
    middleware = telemetry.TelemetryMiddleware()
    calls = []
    ctx = _server_context("clickhouse_run_select_query", calls=calls)

    for i in range(50):
        _run_tool_call(middleware, f"attacker-chosen-{i}", ctx, outcome="raise")

    tags = _new_metric_tags(metrics)
    assert len(tags) == 50
    assert all(
        t
        == {
            "tool": "unknown",
            "success": "false",
            "client_kind": "direct",
            "error_kind": "exception",
        }
        for t in tags
    )
    assert not any("attacker-chosen" in str(t) for t in tags)
    # Pre-existing metrics keep the raw requested name (documented follow-up).
    assert metrics.increment.call_args_list[0].args[1]["tool"] == "attacker-chosen-0"
    # A flood of unknown names re-reads the registered set at most once per window.
    assert len(calls) == 1


def test_registered_tool_keeps_its_real_name(spans, metrics, quiet_caller):
    middleware = telemetry.TelemetryMiddleware()
    calls = []
    ctx = _server_context("clickhouse_run_select_query", "list_studies", calls=calls)

    _run_tool_call(middleware, "list_studies", ctx)
    _run_tool_call(middleware, "clickhouse_run_select_query", ctx)

    assert [t["tool"] for t in _new_metric_tags(metrics)] == [
        "list_studies",
        "list_studies",
        "clickhouse_run_select_query",
        "clickhouse_run_select_query",
    ]
    assert len(calls) == 1  # cached after the first lookup


def test_tool_registered_later_resolves_after_the_refresh_window(
    spans, metrics, quiet_caller, monkeypatch
):
    middleware = telemetry.TelemetryMiddleware()
    names = ["a"]

    async def get_tools():
        return {n: object() for n in names}

    ctx = SimpleNamespace(fastmcp=SimpleNamespace(get_tools=get_tools))
    clock = [1000.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock[0])

    _run_tool_call(middleware, "a", ctx)
    names.append("b")
    _run_tool_call(middleware, "b", ctx)  # inside the window: not re-read yet
    clock[0] += telemetry._TOOL_NAMES_REFRESH_SECONDS
    _run_tool_call(middleware, "b", ctx)

    totals = [
        c.args[2]["tool"]
        for c in metrics.distribution.call_args_list
        if c.args[0] == "tool.total_duration_ms"
    ]
    assert totals == ["a", "unknown", "b"]


@pytest.mark.parametrize("broken", ["no_context", "server_gone", "get_tools_raises"])
def test_tool_lookup_failure_falls_back_to_unknown_without_failing_the_call(
    spans, metrics, quiet_caller, broken
):
    if broken == "no_context":
        ctx = None
    elif broken == "server_gone":

        class Gone:
            @property
            def fastmcp(self):
                raise RuntimeError("FastMCP instance is no longer available")

        ctx = Gone()
    else:

        async def get_tools():
            raise RuntimeError("boom")

        ctx = SimpleNamespace(fastmcp=SimpleNamespace(get_tools=get_tools))

    _run_tool_call(telemetry.TelemetryMiddleware(), "list_studies", ctx)

    assert [t["tool"] for t in _new_metric_tags(metrics)] == ["unknown", "unknown"]
    assert _distributions(metrics)["tool.duration_ms"][1]["tool"] == "list_studies"


def test_unknown_tool_through_a_real_fastmcp_server(metrics, monkeypatch):
    """FastMCP runs middleware before tool lookup; the new metrics still stay bounded."""
    from fastmcp import Client, FastMCP

    monkeypatch.setattr(telemetry, "_llmobs_tool_span", lambda *args: None)
    mcp = FastMCP("bounded-tags", middleware=[telemetry.TelemetryMiddleware()])

    @mcp.tool()
    def ping() -> str:
        return "pong"

    async def scenario():
        async with Client(mcp) as client:
            await client.call_tool("ping", {})
            await client.call_tool("no-such-tool-1f2e", {}, raise_on_error=False)

    asyncio.run(scenario())

    totals = [
        c.args[2]["tool"]
        for c in metrics.distribution.call_args_list
        if c.args[0] == "tool.total_duration_ms"
    ]
    assert totals == ["ping", "unknown"]
