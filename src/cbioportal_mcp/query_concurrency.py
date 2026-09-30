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
was imported first and its pool has a different size, that call raises rather
than let the server run above the cap.

Both settings may come from the .env file mcp-clickhouse loads at import; the
cap is read from the same file (real environment variables win, as they do
for mcp-clickhouse).

When every worker is busy, a new query waits in the pool's queue. That wait
counts against mcp-clickhouse's query timeout (CLICKHOUSE_MCP_QUERY_TIMEOUT,
default 30s), so a caller gets mcp-clickhouse's "Query timed out" error rather
than hanging. A hung query frees its worker when the client's send/receive
timeout fires (by default the query timeout + 5s).
"""

import importlib.util
import logging
import os
import sys

from dotenv import dotenv_values

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
    path = mcp_clickhouse_dotenv_path()
    return dotenv_values(path) if path else {}


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
    """The actual worker count of mcp-clickhouse's query pool."""
    from mcp_clickhouse.mcp_server import QUERY_EXECUTOR

    return QUERY_EXECUTOR._max_workers


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
        verify_query_pool(cap)
        return cap
    existing = _setting(_WORKERS_ENV, dotenv)
    if existing is not None and existing != str(cap):
        logger.warning("%s=%s is overridden by %s=%s", _WORKERS_ENV, existing, _CAP_ENV, cap)
    os.environ[_WORKERS_ENV] = str(cap)
    return cap
