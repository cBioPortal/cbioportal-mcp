"""Bound how many ClickHouse queries one server process runs at once.

mcp-clickhouse runs every query on its QUERY_EXECUTOR thread pool, sized once
at import from CLICKHOUSE_MCP_MAX_WORKERS. A worker stays busy until its
client.query() call really returns, including after run_query() has given up
on a timed-out query and KILL QUERY failed or was slow. So the pool size bounds
the queries actually executing. A semaphore around run_query() would not: it
is released as soon as run_query() raises its timeout, while the query may
still be running.

CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES (default 4) sets that pool size. It must
be applied before mcp_clickhouse.mcp_server is imported, which is why the
package __init__ calls configure_mcp_clickhouse_workers(). If mcp_clickhouse
was imported first and its pool has a different size, importing the package
only warns (a library consumer may not run queries at all), but the server's
main() calls verify_query_pool() and refuses to start.

Both settings may come from the .env file mcp-clickhouse loads at import. We
read them from the same file with the same python-dotenv semantics, so we see
exactly the values mcp-clickhouse's load_dotenv() puts in the environment
(real environment variables win, and ${VAR} expands against them).

When every worker is busy, a new query waits in the pool's queue. That wait
counts against mcp-clickhouse's query timeout (CLICKHOUSE_MCP_QUERY_TIMEOUT,
default 30s), so a caller gets mcp-clickhouse's "Query timed out" error rather
than hanging. A hung query frees its worker when the client's send/receive
timeout fires (by default the query timeout + 5s).
"""

import concurrent.futures
import importlib.util
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from dotenv.main import DotEnv

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_QUERIES = 4
_CAP_ENV = "CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES"
_WORKERS_ENV = "CLICKHOUSE_MCP_MAX_WORKERS"


def mcp_clickhouse_dotenv_path() -> str:
    """Return the .env file mcp-clickhouse's import-time load_dotenv() loads.

    mcp_server.py calls a bare load_dotenv(), whose find_dotenv() walks up
    from the calling file's directory (or from the cwd in a REPL, debugger or
    frozen app). Running find_dotenv() from code attributed to that same file
    lets python-dotenv make the identical choice, without importing
    mcp_server (which would size its pool) or re-implementing dotenv's rules.
    """
    spec = importlib.util.find_spec("mcp_clickhouse")
    if spec is None or not spec.submodule_search_locations:
        return ""
    server_file = os.path.join(spec.submodule_search_locations[0], "mcp_server.py")
    namespace: dict = {}
    code = compile("from dotenv import find_dotenv\npath = find_dotenv()", server_file, "exec")
    exec(code, namespace)
    return namespace["path"]


def _setting(name: str, dotenv: dict) -> str | None:
    """A real environment variable, else the value from mcp-clickhouse's .env."""
    value = os.environ.get(name)
    return value if value is not None else dotenv.get(name)


def _dotenv() -> dict:
    """The values mcp-clickhouse's load_dotenv() would set from its .env.

    load_dotenv() is DotEnv(path, override=False).set_as_environment_variables()
    (utf-8, interpolation on). Taking the same DotEnv's dict() gives the same
    ${VAR} expansion; real environment variables still take precedence in
    _setting(), just as set_as_environment_variables() skips them. Nothing is
    written to os.environ here. (dotenv_values() would expand with
    override=True semantics, letting .env values shadow real ones.)
    """
    path = mcp_clickhouse_dotenv_path()
    if not path:
        return {}
    return DotEnv(path, encoding="utf-8", interpolate=True, override=False).dict()


def max_concurrent_queries_from_env(dotenv: dict | None = None) -> int:
    raw = _setting(_CAP_ENV, _dotenv() if dotenv is None else dotenv)
    if raw is None:
        return DEFAULT_MAX_CONCURRENT_QUERIES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value >= 1:
        return value
    logger.warning(
        "Invalid %s=%r; using %s", _CAP_ENV, raw, DEFAULT_MAX_CONCURRENT_QUERIES
    )
    return DEFAULT_MAX_CONCURRENT_QUERIES


def query_pool_size() -> int:
    """The actual worker count of mcp-clickhouse's query pool.

    Raises RuntimeError if mcp-clickhouse no longer exposes the pool the way
    0.5.0 does (a module-level ThreadPoolExecutor named QUERY_EXECUTOR), since
    then the cap can't be verified.
    """
    import mcp_clickhouse.mcp_server as mcp_server

    size = getattr(getattr(mcp_server, "QUERY_EXECUTOR", None), "_max_workers", None)
    if not isinstance(size, int):
        raise RuntimeError(
            "Cannot verify the ClickHouse query cap: this mcp-clickhouse version has no "
            "QUERY_EXECUTOR thread pool with _max_workers (checked against 0.5.0). "
            "Check how it bounds concurrent queries and update "
            "cbioportal_mcp/query_concurrency.py, or pin mcp-clickhouse==0.5.0."
        )
    return size


def verify_query_pool(cap: int) -> None:
    """Raise unless mcp-clickhouse's query pool has exactly `cap` workers."""
    actual = query_pool_size()
    if actual != cap:
        raise RuntimeError(
            f"mcp-clickhouse's query pool has {actual} workers, but "
            f"{_CAP_ENV}={cap}; the server would run up to {actual} ClickHouse "
            f"queries at once. mcp_clickhouse.mcp_server was imported before "
            f"cbioportal_mcp, so its pool was sized before the cap could be "
            f"applied. Import cbioportal_mcp first, or set {_WORKERS_ENV}={cap}."
        )


def configure_mcp_clickhouse_workers() -> int:
    """Size mcp-clickhouse's query pool to the configured cap; return the cap."""
    dotenv = _dotenv()
    cap = max_concurrent_queries_from_env(dotenv)
    if "mcp_clickhouse.mcp_server" in sys.modules:
        try:
            verify_query_pool(cap)
        except RuntimeError as exc:
            logger.warning("%s The server will refuse to start.", exc)
        return cap
    existing = _setting(_WORKERS_ENV, dotenv)
    if existing is not None and existing != str(cap):
        logger.warning("%s=%s is overridden by %s=%s", _WORKERS_ENV, existing, _CAP_ENV, cap)
    os.environ[_WORKERS_ENV] = str(cap)
    return cap


# --- Observed query execution ---------------------------------------------------
#
# mcp-clickhouse's run_query() generates the ClickHouse query_id itself and
# throws away QueryResult.summary, and its pool queue wait is indistinguishable
# from execution time. run_query() below is mcp-clickhouse 0.5.0's run_query()
# + execute_query(), statement for statement, on mcp-clickhouse's own pool,
# client cache, active-query registry and KILL QUERY cancellation. The only
# additions are timestamps and copying facts the library already has (query_id,
# summary) into a QueryStats. Nothing about what is sent to ClickHouse changes:
# same query text, same settings (readonly + query_id), same timeout, same
# errors. verify_query_runner() refuses startup if those internals move.

_RUNNER_INTERNALS = (
    "QUERY_EXECUTOR",
    "_max_workers",
    "_active_queries",
    "_active_queries_lock",
    "_ActiveQueryState",
    "_resolve_client_config",
    "_register_active_query",
    "_remove_active_query",
    "_mark_active_query_cancelled",
    "_cancel_query_with_bounded_wait",
    "_acquire_clickhouse_client",
    "_release_client_entry",
    "_evict_cached_client",
    "_is_connection_error",
    "_validate_query_for_destructive_ops",
    "_serialize_tool_result",
    "build_query_settings",
    "get_mcp_config",
    "ToolError",
    "logger",
)

# Integer fields of ClickHouse's X-ClickHouse-Summary header that we record.
SUMMARY_FIELDS = (
    "elapsed_ns",
    "read_rows",
    "read_bytes",
    "result_rows",
    "result_bytes",
    "total_rows_to_read",
)


@dataclass
class QueryStats:
    """Facts about one query, filled in by run_query() for telemetry.

    Times are time.perf_counter() values. The worker thread writes the
    started_at/execute fields; the caller reads them only after the future
    resolves, except on a timeout, where they show how far the query got.
    """

    query_id: str | None = None
    submitted_at: float | None = None
    started_at: float | None = None  # a pool worker picked the query up
    client_acquire_ms: float | None = None
    execute_ms: float | None = None  # client.query(): network + ClickHouse + decode
    serialize_ms: float | None = None  # mcp-clickhouse's JSON serialization
    returned_at: float | None = None  # the caller got the serialized result back
    rows_returned: int | None = None
    payload_chars: int | None = None
    timed_out: bool = False
    server: dict = field(default_factory=dict)  # SUMMARY_FIELDS ClickHouse reported

    @property
    def queue_wait_ms(self) -> float | None:
        if self.submitted_at is None or self.started_at is None:
            return None
        return (self.started_at - self.submitted_at) * 1000


_current_stats: ContextVar[QueryStats | None] = ContextVar(
    "cbioportal_mcp_query_stats", default=None
)


@contextmanager
def recording_query_stats(stats: QueryStats) -> Iterator[QueryStats]:
    """Have run_query() calls in this context record into ``stats``."""
    token = _current_stats.set(stats)
    try:
        yield stats
    finally:
        _current_stats.reset(token)


def _summary_stats(summary) -> dict:
    out = {}
    if not isinstance(summary, dict):
        return out
    for name in SUMMARY_FIELDS:
        try:
            out[name] = int(summary[name])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def verify_query_runner() -> None:
    """Raise unless mcp-clickhouse exposes what run_query() is built on."""
    import mcp_clickhouse.mcp_server as mcp_server

    missing = [name for name in _RUNNER_INTERNALS if not hasattr(mcp_server, name)]
    if missing:
        raise RuntimeError(
            "cbioportal_mcp.query_concurrency.run_query mirrors mcp-clickhouse 0.5.0's "
            f"run_query, but this mcp-clickhouse lacks {', '.join(missing)}. "
            "Re-check run_query against the new version or pin mcp-clickhouse==0.5.0."
        )


def _execute_query(query: str, query_id: str, client_config, stats: QueryStats) -> str:
    """mcp-clickhouse 0.5.0's execute_query(), recording into ``stats``."""
    import mcp_clickhouse.mcp_server as mc

    stats.started_at = time.perf_counter()
    with mc._active_queries_lock:
        state = mc._active_queries.get(query_id)
        if state is None:
            state = mc._ActiveQueryState(query=query)
            mc._active_queries[query_id] = state

    entry = None
    try:
        entry = mc._acquire_clickhouse_client(client_config)
        client = entry.client
        stats.client_acquire_ms = (time.perf_counter() - stats.started_at) * 1000
        with mc._active_queries_lock:
            if state.cancelled:
                raise mc.ToolError("Query cancelled before execution")
            state.client_entry = entry

        mc._validate_query_for_destructive_ops(query)

        query_settings = mc.build_query_settings(client)
        query_settings["query_id"] = query_id
        with mc._active_queries_lock:
            if state.cancelled:
                raise mc.ToolError("Query cancelled before execution")
        execute_started = time.perf_counter()
        res = client.query(query, settings=query_settings)
        stats.execute_ms = (time.perf_counter() - execute_started) * 1000
        stats.server = _summary_stats(getattr(res, "summary", None))
        stats.rows_returned = len(res.result_rows)
        mc.logger.info(f"Query {query_id} returned {len(res.result_rows)} rows")
        serialize_started = time.perf_counter()
        payload = mc._serialize_tool_result({"columns": res.column_names, "rows": res.result_rows})
        stats.serialize_ms = (time.perf_counter() - serialize_started) * 1000
        stats.payload_chars = len(payload)
        return payload
    except mc.ToolError:
        raise
    except Exception as err:
        # Do not retry queries because a write may already have succeeded.
        if entry is not None and mc._is_connection_error(err):
            mc._evict_cached_client(client_config, client)
        mc.logger.error(f"Error executing query {query_id}: {err}")
        raise mc.ToolError(f"Query execution failed: {str(err)}")  # noqa: B904 (as upstream)
    finally:
        mc._remove_active_query(query_id, state)
        if entry is not None:
            mc._release_client_entry(entry)


def run_query(query: str) -> str:
    """Run ``query`` exactly as mcp-clickhouse 0.5.0's run_query() does.

    Same pool, client cache, readonly settings, CLICKHOUSE_MCP_QUERY_TIMEOUT
    (queue wait included), KILL QUERY on timeout, and the same ToolError /
    RuntimeError messages. The difference: the query_id, queue wait, execution
    time and ClickHouse's summary are recorded into the QueryStats of the
    enclosing recording_query_stats() block, if any.
    """
    import mcp_clickhouse.mcp_server as mc

    stats = _current_stats.get()
    if stats is None:
        stats = QueryStats()

    mc.logger.info(f"Executing query: {query}")

    client_config = mc._resolve_client_config()
    query_id = str(uuid.uuid4())
    stats.query_id = query_id
    state = mc._register_active_query(query_id, query)

    try:
        with mc._active_queries_lock:
            in_flight = len(mc._active_queries)
        if in_flight >= mc._max_workers:
            mc.logger.warning(
                "Thread pool saturated: %d in-flight vs %d workers",
                in_flight,
                mc._max_workers,
            )

        try:
            stats.submitted_at = time.perf_counter()
            future = mc.QUERY_EXECUTOR.submit(_execute_query, query, query_id, client_config, stats)
        except Exception:
            mc._remove_active_query(query_id, state)
            raise
        timeout_secs = mc.get_mcp_config().query_timeout
        try:
            result = future.result(timeout=timeout_secs)
            stats.returned_at = time.perf_counter()
            return result
        except concurrent.futures.TimeoutError:
            stats.timed_out = True
            mc.logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            if future.cancel():
                mc._remove_active_query(query_id, state)
            else:
                mc._mark_active_query_cancelled(query_id)
                mc._cancel_query_with_bounded_wait(query_id)
            raise mc.ToolError(f"Query timed out after {timeout_secs} seconds")  # noqa: B904
    except mc.ToolError:
        raise
    except Exception as e:
        mc.logger.error("Unexpected error in run_query: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")  # noqa: B904
