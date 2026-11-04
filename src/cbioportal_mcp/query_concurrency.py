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

import importlib.util
import logging
import os
import sys

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
