"""Concurrent tool calls overlap instead of running one after another.

FastMCP 2.x calls a sync tool function directly on the event loop, so a batch
of tool calls sent concurrently used to cost the sum of its calls. Tools that
do I/O are now wrapped with server.run_off_event_loop. These tests drive the
real FastMCP server through an in-process client.

How many ClickHouse queries execute at once is bounded by mcp-clickhouse's
query pool, sized from CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES (see
cbioportal_mcp.query_concurrency).

The real-ClickHouse tests start a native `clickhouse server` (no Docker) and
are skipped when the binary isn't installed.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import mcp_clickhouse.mcp_server
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cbioportal_mcp import query_concurrency, server
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
    "list_guides",
    "read_guide",
    "get_general_guide",
    "list_study_guides",
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


class FakeClickHouseClient:
    """A clickhouse-connect stand-in behind mcp-clickhouse's real run_query().

    query() sleeps for `delay`, or blocks until `release` is set when `hang`
    is true. KILL QUERY (command()) fails outright or takes longer than
    mcp-clickhouse's 1s bounded cancellation wait. Records the peak number of
    query() calls executing at once, which is what the cap has to bound.
    """

    server_settings = {}

    def __init__(self, *, delay=0.0, hang=False, kill="fail"):
        self.delay = delay
        self.hang = hang
        self.kill = kill
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.executing = 0
        self.peak = 0
        self.started = 0
        self.kills = 0

    def query(self, query, settings=None):
        with self.lock:
            self.executing += 1
            self.started += 1
            self.peak = max(self.peak, self.executing)
        try:
            if self.hang:
                self.release.wait(30)
            else:
                time.sleep(self.delay)
            return SimpleNamespace(column_names=["n"], result_rows=[[1]])
        finally:
            with self.lock:
                self.executing -= 1

    def command(self, sql):
        assert sql.startswith("KILL QUERY")
        with self.lock:
            self.kills += 1
        if self.kill == "fail":
            raise RuntimeError("KILL QUERY failed")
        time.sleep(3)


@pytest.fixture
def query_pool(monkeypatch):
    """Point mcp-clickhouse at a fake client and a query pool of a given size.

    The pool stands in for the QUERY_EXECUTOR that
    configure_mcp_clickhouse_workers() sizes in production (proven by
    test_package_import_sizes_mcp_clickhouse_query_pool).
    """
    ms = mcp_clickhouse.mcp_server
    monkeypatch.setenv("CLICKHOUSE_HOST", "fake-clickhouse")
    monkeypatch.setenv("CLICKHOUSE_USER", "fake")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "fake")
    monkeypatch.setenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "1")
    pools = []

    def install(cap, client):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=cap)
        pools.append((pool, client))
        monkeypatch.setattr(ms, "QUERY_EXECUTOR", pool)
        monkeypatch.setattr(
            ms,
            "_acquire_clickhouse_client",
            lambda _config: ms._ClientCacheEntry(
                client=client, last_used=time.time(), active_users=1
            ),
        )
        return client

    server._clear_schema_cache()
    yield install
    for pool, client in pools:
        client.release.set()
        pool.shutdown(wait=True)
    server._clear_schema_cache()


def test_cap_bounds_queries_in_flight(query_pool, monkeypatch):
    # Queue wait counts against the query timeout, so leave room for 3 rounds.
    monkeypatch.setenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "5")
    client = query_pool(2, FakeClickHouseClient(delay=DELAY))
    elapsed, results = asyncio.run(
        _timed_batch(server.mcp, [_select(f"SELECT {i}") for i in range(5)])
    )
    assert all(r.structured_content == {"rows": [{"n": 1}]} for r in results)
    assert client.peak == 2
    # 5 queries, 2 at a time: three rounds, not one and not five.
    assert 3 * DELAY * 0.95 <= elapsed < 4 * DELAY + 0.5


_POOL_ENV = ("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES", "CLICKHOUSE_MCP_MAX_WORKERS")


def _fresh_python(code, env, cwd, *args):
    """Run `python -c code` in a fresh process with the pool settings cleared.

    With `-c`, python-dotenv's find_dotenv() (used by mcp-clickhouse's
    import-time load_dotenv()) searches upward from the cwd, so a .env in the
    test's cwd is the file mcp-clickhouse loads.
    """
    clean = {k: v for k, v in os.environ.items() if k not in _POOL_ENV}
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env={**clean, **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


# Runs in a fresh process so the server is configured exactly as in production:
# CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES is read at import and mcp-clickhouse's
# real QUERY_EXECUTOR is used; only the ClickHouse client is faked.
_HUNG_QUERY_SCENARIO = """
import asyncio, json, sys, time
sys.path.insert(0, {tests_dir!r})
{preamble}
from cbioportal_mcp import server
import mcp_clickhouse.mcp_server as ms
from fastmcp import Client
from test_parallel_tool_exec import FakeClickHouseClient

mode = sys.argv[1]
client = FakeClickHouseClient(hang=True, kill="fail" if mode == "sequential" else "slow")
ms._acquire_clickhouse_client = lambda _config: ms._ClientCacheEntry(
    client=client, last_used=time.time(), active_users=1
)
select = server.clickhouse_run_select_query.fn.__wrapped__
errors, durations = [], []
started = time.perf_counter()
if mode == "sequential":
    for i in range(3):
        t = time.perf_counter()
        errors.append(select(f"SELECT {{i}}").get("error_message", ""))
        durations.append(time.perf_counter() - t)
else:
    async def batch():
        async with Client(server.mcp) as c:
            return await asyncio.gather(*(
                c.call_tool("clickhouse_run_select_query", {{"query": f"SELECT {{i}}"}})
                for i in range(5)
            ))
    errors = [r.structured_content.get("error_message", "") for r in asyncio.run(batch())]
elapsed = time.perf_counter() - started
after_timeouts = {{"peak": client.peak, "started": client.started, "kills": client.kills}}
client.release.set()
client.hang = False
deadline = time.monotonic() + 5
while client.executing and time.monotonic() < deadline:
    time.sleep(0.05)
recovered = select("SELECT 99")
print(json.dumps({{
    **after_timeouts, "pool": ms.QUERY_EXECUTOR._max_workers, "peak_final": client.peak,
    "errors": errors, "durations": durations, "elapsed": elapsed, "recovered": recovered,
}}))
"""


def _run_hung_query_scenario(tmp_path, mode, env, preamble=""):
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    env = {
        "CLICKHOUSE_MCP_QUERY_TIMEOUT": "1",
        "CLICKHOUSE_HOST": "fake-clickhouse",
        "CLICKHOUSE_USER": "fake",
        "CLICKHOUSE_PASSWORD": "fake",
        **env,
    }
    code = _HUNG_QUERY_SCENARIO.format(tests_dir=tests_dir, preamble=preamble)
    done = _fresh_python(code, env, tmp_path, mode)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_timed_out_queries_keep_their_slot_until_they_stop(tmp_path):
    """Regression: the cap must follow the query that is really executing.

    cap=1: the first query times out and survives a failed KILL QUERY, so it
    keeps running. The next two calls must not start queries beside it; they
    wait in the pool, then fail with mcp-clickhouse's timeout instead of
    hanging. Once the hung query stops, its slot is free again.
    """
    out = _run_hung_query_scenario(
        tmp_path, "sequential", {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "1"}
    )
    assert all("timed out" in e for e in out["errors"]), out
    assert out["started"] == 1 and out["peak"] == 1 and out["kills"] == 1, out
    assert all(d < 4 for d in out["durations"]), out
    assert out["recovered"] == {"rows": [{"n": 1}]}
    assert out["peak_final"] == 1


def test_concurrent_batch_with_slow_kill_never_exceeds_the_cap(tmp_path):
    """cap=2, five concurrent calls through the MCP client, every query hangs
    and KILL QUERY is slower than mcp-clickhouse's cancellation wait."""
    out = _run_hung_query_scenario(
        tmp_path, "batch", {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "2"}
    )
    assert all("timed out" in e for e in out["errors"]), out
    assert out["started"] == 2 and out["peak"] == 2, out
    # Bounded: the 1s query timeout plus mcp-clickhouse's 1s cancellation wait.
    assert out["elapsed"] < 4, out
    assert out["recovered"] == {"rows": [{"n": 1}]}
    assert out["peak_final"] == 2


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
    with caplog.at_level(logging.WARNING, logger=query_concurrency.logger.name):
        assert query_concurrency.max_concurrent_queries_from_env(dotenv={}) == expected
    assert ("Invalid CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES" in caplog.text) == warns


# Prints the pool size, whether mcp-clickhouse loaded the .env (a sentinel it
# sets), and the cap as it ends up in the environment after that load.
_POOL_SIZE = (
    "import cbioportal_mcp, os, mcp_clickhouse.mcp_server as ms; "
    "print(ms.QUERY_EXECUTOR._max_workers, os.environ.get('DOTENV_SENTINEL'), "
    "os.environ.get('CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES'))"
)


def _pool_size_in_fresh_process(tmp_path, env, dotenv=None):
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    done = _fresh_python(_POOL_SIZE, env, tmp_path)
    assert done.returncode == 0, done.stderr
    size, sentinel, env_cap = done.stdout.strip().splitlines()[-1].split()
    return int(size), sentinel, env_cap, done.stderr


@pytest.mark.parametrize(
    "env, expected, warning",
    [
        ({}, 4, None),
        ({"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "3"}, 3, None),
        (
            {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "3", "CLICKHOUSE_MCP_MAX_WORKERS": "10"},
            3,
            "CLICKHOUSE_MCP_MAX_WORKERS=10 is overridden",
        ),
    ],
)
def test_package_import_sizes_mcp_clickhouse_query_pool(tmp_path, env, expected, warning):
    size, _, _, stderr = _pool_size_in_fresh_process(tmp_path, env)
    assert size == expected
    if warning:
        assert warning in stderr


@pytest.mark.parametrize(
    "dotenv, env, expected, warning",
    [
        # Only .env sets the cap.
        ("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=1\n", {}, 1, None),
        # A real environment variable wins over .env, as it does for mcp-clickhouse.
        (
            "CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=1\n",
            {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "2"},
            2,
            None,
        ),
        # mcp-clickhouse's own setting in .env is still overridden by the cap.
        ("CLICKHOUSE_MCP_MAX_WORKERS=10\n", {}, 4, "CLICKHOUSE_MCP_MAX_WORKERS=10 is overridden"),
    ],
)
def test_cap_is_read_from_the_dotenv_mcp_clickhouse_loads(
    tmp_path, dotenv, env, expected, warning
):
    size, sentinel, _, stderr = _pool_size_in_fresh_process(
        tmp_path, env, dotenv + "DOTENV_SENTINEL=loaded\n"
    )
    assert size == expected
    # mcp-clickhouse's own load_dotenv() loaded this same file.
    assert sentinel == "loaded"
    if warning:
        assert warning in stderr


@pytest.mark.parametrize(
    "env, expected",
    [
        # Regression: a real POOL_VALUE wins over the .env one when ${POOL_VALUE}
        # expands, exactly as in mcp-clickhouse's load_dotenv(override=False).
        ({"POOL_VALUE": "1"}, 1),
        # Without a real one, the .env's own POOL_VALUE is used.
        ({}, 8),
    ],
)
def test_dotenv_interpolation_matches_what_mcp_clickhouse_loads(tmp_path, env, expected):
    assert "POOL_VALUE" in env or "POOL_VALUE" not in os.environ
    size, sentinel, env_cap, _ = _pool_size_in_fresh_process(
        tmp_path,
        env,
        "POOL_VALUE=8\n"
        "CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=${POOL_VALUE}\n"
        "DOTENV_SENTINEL=loaded\n",
    )
    assert sentinel == "loaded"
    # The cap we applied equals the one mcp-clickhouse put in the environment.
    assert env_cap == str(expected)
    assert size == expected


def test_dotenv_resolution_matches_mcp_clickhouse():
    """Outside `-c`/REPL mode, find_dotenv() walks up from mcp_server.py's
    directory; our resolver must pick the same file (or none)."""
    import mcp_clickhouse

    start = os.path.dirname(mcp_clickhouse.__file__)
    expected = ""
    current = start
    while True:
        candidate = os.path.join(current, ".env")
        if os.path.isfile(candidate):
            expected = candidate
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    assert query_concurrency.mcp_clickhouse_dotenv_path() == expected


def test_script_mode_ignores_a_cwd_dotenv_like_mcp_clickhouse(tmp_path):
    """Run as a script (the console entry point's mode), find_dotenv() starts
    from mcp_server.py's directory, not the cwd. A .env only in the cwd is
    ignored by mcp-clickhouse, so the cap must ignore it too."""
    (tmp_path / ".env").write_text(
        "CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=1\nDOTENV_SENTINEL=loaded\n"
    )
    script = tmp_path / "probe.py"
    script.write_text(_POOL_SIZE.replace("; ", "\n"))
    clean = {k: v for k, v in os.environ.items() if k not in _POOL_ENV}
    done = subprocess.run(
        [sys.executable, str(script)], env=clean, cwd=tmp_path,
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    size, sentinel, _ = done.stdout.strip().splitlines()[-1].split()
    expected_path = query_concurrency.mcp_clickhouse_dotenv_path()
    if expected_path:
        pytest.skip(f"a .env above the installed mcp_clickhouse applies: {expected_path}")
    assert sentinel == "None"
    assert int(size) == 4


_MAIN_WITH_MCP_CLICKHOUSE_FIRST = """
import mcp_clickhouse.mcp_server
from cbioportal_mcp import server
server.main()
"""


def test_server_refuses_to_start_on_a_mis_sized_pool(tmp_path):
    """mcp_clickhouse imported first sizes its pool to its default of 10; with a
    cap of 1 the server entry point must exit, naming the real size and fixes."""
    done = _fresh_python(
        _MAIN_WITH_MCP_CLICKHOUSE_FIRST,
        {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "1"},
        tmp_path,
    )
    assert done.returncode == 2, done.stderr[-2000:]
    assert "mcp-clickhouse's query pool has 10 workers" in done.stderr
    assert "CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=1" in done.stderr
    assert "set CLICKHOUSE_MCP_MAX_WORKERS=1" in done.stderr
    assert "ClickHouse query pool:" not in done.stderr


def test_library_import_after_mcp_clickhouse_only_warns(tmp_path):
    """A library consumer that imported mcp_clickhouse first can still import
    the package; the warning names the pool's actual size."""
    done = _fresh_python(
        "import mcp_clickhouse.mcp_server\n"
        "import cbioportal_mcp.telemetry\n"
        "import cbioportal_mcp.server\n"
        "print('imported')",
        {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "1"},
        tmp_path,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("imported")
    assert "query pool has 10 workers" in done.stderr
    assert "The server will refuse to start." in done.stderr


def test_importing_mcp_clickhouse_first_with_a_matching_pool_stays_capped(tmp_path):
    """The documented fix for that error: size mcp-clickhouse's pool to the cap.
    Then the hung-query scenario still never runs above the cap."""
    out = _run_hung_query_scenario(
        tmp_path,
        "sequential",
        {"CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES": "1", "CLICKHOUSE_MCP_MAX_WORKERS": "1"},
        preamble="import mcp_clickhouse.mcp_server",
    )
    assert out["pool"] == 1
    assert out["started"] == 1 and out["peak"] == 1 and out["peak_final"] == 1, out


def test_late_configuration_checks_the_actual_pool(monkeypatch, caplog):
    ms = mcp_clickhouse.mcp_server
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES", "3")
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_WORKERS", "3")  # env agrees; the pool doesn't
    wrong = concurrent.futures.ThreadPoolExecutor(max_workers=7)
    monkeypatch.setattr(ms, "QUERY_EXECUTOR", wrong)
    with caplog.at_level(logging.WARNING, logger=query_concurrency.logger.name):
        assert query_concurrency.configure_mcp_clickhouse_workers() == 3
    assert "query pool has 7 workers" in caplog.text
    with pytest.raises(RuntimeError, match="query pool has 7 workers"):
        query_concurrency.verify_query_pool(3)
    right = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    monkeypatch.setattr(ms, "QUERY_EXECUTOR", right)
    query_concurrency.verify_query_pool(3)
    wrong.shutdown()
    right.shutdown()


@pytest.mark.parametrize("pool", [None, object()], ids=["no-QUERY_EXECUTOR", "no-_max_workers"])
def test_unrecognised_mcp_clickhouse_pool_stops_the_server(monkeypatch, caplog, pool):
    ms = mcp_clickhouse.mcp_server
    if pool is None:
        monkeypatch.delattr(ms, "QUERY_EXECUTOR")
    else:
        monkeypatch.setattr(ms, "QUERY_EXECUTOR", pool)
    with pytest.raises(RuntimeError, match="Cannot verify the ClickHouse query cap"):
        query_concurrency.query_pool_size()
    with caplog.at_level(logging.CRITICAL, logger=server.logger.name):
        with pytest.raises(SystemExit) as exit_info:
            server.main()
    assert exit_info.value.code == 2
    assert "pin mcp-clickhouse==0.5.0" in caplog.text


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


def _real_query_pool(monkeypatch, size):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=size)
    monkeypatch.setattr(mcp_clickhouse.mcp_server, "QUERY_EXECUTOR", pool)
    return pool


def test_concurrent_queries_against_real_clickhouse(real_clickhouse, monkeypatch):
    _real_query_pool(monkeypatch, N_REAL)
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
    _real_query_pool(monkeypatch, 4)
    calls = [_select(f"SELECT {i} AS i, sleep({DELAY}) AS s") for i in range(N_REAL)]
    elapsed, results = asyncio.run(_timed_batch(server.mcp, calls))
    assert all("rows" in r.structured_content for r in results)
    # 8 queries, 4 at a time: two rounds.
    assert 2 * DELAY * 0.95 <= elapsed < N_REAL * DELAY
