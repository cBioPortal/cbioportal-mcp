"""Concurrent tool calls overlap instead of running one after another.

FastMCP 2.x calls a sync tool function directly on the event loop, so a batch
of tool calls sent concurrently used to cost the sum of its calls. Tools that
do I/O are now wrapped with server.run_off_event_loop. These tests drive the
real FastMCP server through an in-process client.

The real-ClickHouse tests start a native `clickhouse server` (no Docker) and
are skipped when the binary isn't installed.
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from unittest.mock import patch

import mcp_clickhouse.mcp_server
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cbioportal_mcp import server
from cbioportal_mcp.telemetry import TelemetryMiddleware

DELAY = 0.4

OFFLOADED_TOOLS = [
    "clickhouse_run_select_query",
    "clickhouse_list_tables",
    "clickhouse_list_table_columns",
    "get_study_guide",
    "list_studies",
    "search_oncotree",
    "get_alteration_frequency",
    "get_top_altered_genes",
    "get_gene_frequency_by_cancer_type",
    "get_profiled_counts",
]


class SlowClickHouse:
    """Stand-in for mcp-clickhouse's run_query: sleeps, then returns one row.

    Records the peak number of queries in flight at once, and the FastMCP
    request id each query ran under.
    """

    def __init__(self, delay=DELAY, fail_on=None):
        self.delay = delay
        self.fail_on = fail_on
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.request_ids = []

    def __call__(self, query):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.request_ids.append(get_context().request_id)
        try:
            time.sleep(self.delay)
            if self.fail_on and self.fail_on in query:
                raise RuntimeError("boom from ClickHouse")
            return json.dumps({"columns": ["n"], "rows": [[1]]})
        finally:
            with self.lock:
                self.in_flight -= 1


@pytest.fixture
def slow_clickhouse(monkeypatch):
    stub = SlowClickHouse()
    monkeypatch.setattr(mcp_clickhouse.mcp_server, "run_query", stub)
    server._clear_schema_cache()
    yield stub
    server._clear_schema_cache()


async def _timed_batch(mcp, calls):
    async with Client(mcp) as client:
        started = time.perf_counter()
        results = await asyncio.gather(
            *(client.call_tool(name, args, raise_on_error=False) for name, args in calls)
        )
        return time.perf_counter() - started, results


def _select(query):
    return ("clickhouse_run_select_query", {"query": query})


def test_sync_tools_run_one_after_another_on_the_event_loop():
    """The behaviour being fixed: FastMCP runs a plain sync tool on the event
    loop, so two concurrent calls take the sum of their durations."""
    mcp = FastMCP("control")

    @mcp.tool()
    def blocking() -> str:
        time.sleep(DELAY)
        return "ok"

    elapsed, _ = asyncio.run(_timed_batch(mcp, [("blocking", {}), ("blocking", {})]))
    assert elapsed >= 2 * DELAY * 0.95


@pytest.mark.parametrize("name", OFFLOADED_TOOLS)
def test_io_tools_are_registered_as_async(name):
    tool = asyncio.run(server.mcp.get_tools())[name]
    assert asyncio.iscoroutinefunction(tool.fn)
    assert not asyncio.iscoroutinefunction(tool.fn.__wrapped__)


def test_two_concurrent_slow_calls_take_about_the_slowest(slow_clickhouse):
    elapsed, results = asyncio.run(
        _timed_batch(server.mcp, [_select("SELECT 1"), ("clickhouse_list_tables", {})])
    )
    assert all(not r.is_error for r in results)
    assert results[0].structured_content == {"rows": [{"n": 1}]}
    assert slow_clickhouse.peak == 2
    assert elapsed < 1.5 * DELAY, f"batch took {elapsed:.2f}s; calls did not overlap"


def test_each_query_sees_its_own_request_context(slow_clickhouse):
    """contextvars (the FastMCP request context mcp-clickhouse reads per-request
    client settings from) follow each call onto its worker thread."""
    asyncio.run(_timed_batch(server.mcp, [_select("SELECT 1"), _select("SELECT 2")]))
    ids = slow_clickhouse.request_ids
    assert len(ids) == 2 and all(ids) and ids[0] != ids[1]


def test_a_failing_call_does_not_affect_the_others(slow_clickhouse):
    slow_clickhouse.fail_on = "boom"
    elapsed, results = asyncio.run(
        _timed_batch(
            server.mcp,
            [_select("SELECT 1"), _select("SELECT 'boom'"), _select("SELECT 2")],
        )
    )
    ok_1, failed, ok_2 = (r.structured_content for r in results)
    assert ok_1 == {"rows": [{"n": 1}]} and ok_2 == {"rows": [{"n": 1}]}
    assert "boom from ClickHouse" in failed["error_message"]
    assert elapsed < 1.5 * DELAY


def test_a_raising_tool_fails_alone(slow_clickhouse, monkeypatch):
    """An exception escaping a tool body surfaces as that call's error only."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("oncotree exploded")

    monkeypatch.setattr(server, "_load_oncotree_data", explode)
    _, results = asyncio.run(
        _timed_batch(
            server.mcp, [("search_oncotree", {"search_term": "BRCA"}), _select("SELECT 1")]
        )
    )
    assert results[0].is_error and "oncotree exploded" in results[0].content[0].text
    assert results[1].structured_content == {"rows": [{"n": 1}]}


def test_semaphore_bounds_queries_in_flight(slow_clickhouse, monkeypatch):
    monkeypatch.setattr(server, "_query_slots", threading.BoundedSemaphore(2))
    elapsed, results = asyncio.run(
        _timed_batch(server.mcp, [_select(f"SELECT {i}") for i in range(5)])
    )
    assert all(r.structured_content == {"rows": [{"n": 1}]} for r in results)
    assert slow_clickhouse.peak == 2
    # 5 queries, 2 at a time: three rounds, not one and not five.
    assert 3 * DELAY * 0.95 <= elapsed < 4 * DELAY


def test_configured_cap_applies_to_tool_calls(slow_clickhouse):
    asyncio.run(_timed_batch(server.mcp, [_select(f"SELECT {i}") for i in range(8)]))
    assert slow_clickhouse.peak == min(8, server.MAX_CONCURRENT_QUERIES)


@pytest.mark.parametrize(
    "raw, expected, warns",
    [(None, 4, False), ("8", 8, False), ("1", 1, False),
     ("0", 4, True), ("-3", 4, True), ("four", 4, True), ("", 4, True)],
)
def test_max_concurrent_queries_env(monkeypatch, caplog, raw, expected, warns):
    if raw is None:
        monkeypatch.delenv("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES", raising=False)
    else:
        monkeypatch.setenv("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES", raw)
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        assert server._max_concurrent_queries_from_env() == expected
    assert ("Invalid CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES" in caplog.text) == warns


def test_db_spans_attach_to_their_own_tool_span(slow_clickhouse):
    """Two overlapping calls: each db.query span is a child of its own
    mcp.tool span, not of whichever call happened to start last."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    with patch("cbioportal_mcp.telemetry.trace.get_tracer", return_value=tracer), patch(
        "cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None
    ):
        middleware = TelemetryMiddleware()
        server.mcp.add_middleware(middleware)
        try:
            asyncio.run(
                _timed_batch(
                    server.mcp,
                    [_select("SELECT 1"), ("get_profiled_counts", {"study_id": "s1"})],
                )
            )
        finally:
            server.mcp.middleware.remove(middleware)

    spans = exporter.get_finished_spans()
    tool_spans = {s.name: s for s in spans if s.name.startswith("mcp.tool/")}
    db_spans = [s for s in spans if s.name.startswith("db.query/")]
    assert set(tool_spans) == {
        "mcp.tool/clickhouse_run_select_query",
        "mcp.tool/get_profiled_counts",
    }
    parent_of = {s.name: s.parent.span_id for s in db_spans}
    select_tool = tool_spans["mcp.tool/clickhouse_run_select_query"].context
    counts_tool = tool_spans["mcp.tool/get_profiled_counts"].context
    assert parent_of["db.query/clickhouse_run_select_query"] == select_tool.span_id
    counts_parents = {
        v for k, v in parent_of.items() if k.startswith("db.query/domain_tools.profiled_counts")
    }
    assert counts_parents == {counts_tool.span_id}
    assert all(s.context.trace_id in {select_tool.trace_id, counts_tool.trace_id} for s in db_spans)


# --- real ClickHouse ------------------------------------------------------

CLICKHOUSE_BIN = shutil.which("clickhouse") or (
    "/opt/homebrew/bin/clickhouse" if os.path.exists("/opt/homebrew/bin/clickhouse") else None
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def local_clickhouse(tmp_path_factory):
    if CLICKHOUSE_BIN is None:
        pytest.skip("native clickhouse binary not installed")
    root = tmp_path_factory.mktemp("clickhouse")
    (root / "data").mkdir()
    http_port, tcp_port, mysql_port, pg_port, interserver_port = (
        _free_port() for _ in range(5)
    )
    proc = subprocess.Popen(
        [
            CLICKHOUSE_BIN, "server", "--",
            f"--http_port={http_port}", f"--tcp_port={tcp_port}",
            f"--mysql_port={mysql_port}", f"--postgresql_port={pg_port}",
            f"--interserver_http_port={interserver_port}",
            "--listen_host=127.0.0.1", f"--path={root / 'data'}/",
            "--logger.console=0", f"--logger.log={root / 'server.log'}",
            f"--logger.errorlog={root / 'error.log'}",
        ],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 180
    try:
        while True:
            if proc.poll() is not None:
                pytest.skip(f"clickhouse server exited early; see {root / 'server.log'}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/ping", timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.skip("clickhouse server did not start within 180s")
                time.sleep(0.5)
        yield http_port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def real_clickhouse(local_clickhouse, monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", str(local_clickhouse))
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "default")
    mcp_clickhouse.mcp_server._clear_client_cache()
    yield local_clickhouse
    mcp_clickhouse.mcp_server._clear_client_cache()


def test_a_session_client_cannot_run_concurrent_queries(real_clickhouse):
    """Control: the failure mode being guarded against is real. One
    clickhouse-connect client with a session id rejects overlapping queries."""
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host="127.0.0.1", port=real_clickhouse, username="default",
        autogenerate_session_id=True,
    )
    errors = []

    def query():
        try:
            client.query("SELECT sleep(0.3)")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=query) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.close()
    assert errors and all("session" in str(e).lower() for e in errors)


N_REAL = 8


def test_concurrent_queries_against_real_clickhouse(real_clickhouse, monkeypatch):
    monkeypatch.setattr(server, "_query_slots", threading.BoundedSemaphore(N_REAL))
    calls = [_select(f"SELECT {i} AS i, sleep({DELAY}) AS s") for i in range(N_REAL)]
    elapsed, results = asyncio.run(_timed_batch(server.mcp, calls))

    rows = [r.structured_content for r in results]
    assert all("error_message" not in r for r in rows), rows
    assert sorted(r["rows"][0]["i"] for r in rows) == list(range(N_REAL))
    assert elapsed < N_REAL * DELAY / 2, f"{N_REAL} queries took {elapsed:.2f}s"

    # mcp-clickhouse shares one cached client across the calls, and that
    # client has no ClickHouse session, so overlapping queries can't collide.
    entries = list(mcp_clickhouse.mcp_server._client_cache.values())
    assert entries
    assert all(e.client.params.get("session_id") is None for e in entries)


def test_cap_bounds_real_queries(real_clickhouse, monkeypatch):
    monkeypatch.setattr(server, "_query_slots", threading.BoundedSemaphore(4))
    calls = [_select(f"SELECT {i} AS i, sleep({DELAY}) AS s") for i in range(N_REAL)]
    elapsed, results = asyncio.run(_timed_batch(server.mcp, calls))
    assert all("rows" in r.structured_content for r in results)
    # 8 queries, 4 at a time: two rounds.
    assert 2 * DELAY * 0.95 <= elapsed < N_REAL * DELAY
