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
package __init__ calls configure_mcp_clickhouse_workers().

When every worker is busy, a new query waits in the pool's queue. That wait
counts against mcp-clickhouse's query timeout (CLICKHOUSE_MCP_QUERY_TIMEOUT,
default 30s), so a caller gets mcp-clickhouse's "Query timed out" error rather
than hanging. A hung query frees its worker when the client's send/receive
timeout fires (by default the query timeout + 5s).
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_QUERIES = 4
_WORKERS_ENV = "CLICKHOUSE_MCP_MAX_WORKERS"


def max_concurrent_queries_from_env() -> int:
    raw = os.getenv("CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES")
    if raw is None:
        return DEFAULT_MAX_CONCURRENT_QUERIES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value >= 1:
        return value
    logger.warning(
        "Invalid CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=%r; using %s",
        raw,
        DEFAULT_MAX_CONCURRENT_QUERIES,
    )
    return DEFAULT_MAX_CONCURRENT_QUERIES


MAX_CONCURRENT_QUERIES = max_concurrent_queries_from_env()


def configure_mcp_clickhouse_workers() -> None:
    """Size mcp-clickhouse's query pool to MAX_CONCURRENT_QUERIES."""
    if "mcp_clickhouse.mcp_server" in sys.modules:
        logger.warning(
            "mcp_clickhouse was imported before cbioportal_mcp; its query pool keeps "
            "%s=%s instead of CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=%s",
            _WORKERS_ENV,
            os.getenv(_WORKERS_ENV, "10"),
            MAX_CONCURRENT_QUERIES,
        )
        return
    existing = os.getenv(_WORKERS_ENV)
    if existing is not None and existing != str(MAX_CONCURRENT_QUERIES):
        logger.warning(
            "%s=%s is overridden by CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=%s",
            _WORKERS_ENV,
            existing,
            MAX_CONCURRENT_QUERIES,
        )
    os.environ[_WORKERS_ENV] = str(MAX_CONCURRENT_QUERIES)
