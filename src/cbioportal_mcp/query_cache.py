"""Opt-in SELECT caching using the pinned mcp-clickhouse 0.5.0 worker API."""

import os
import uuid
from concurrent.futures import TimeoutError


def query_cache_settings() -> dict[str, int | str] | None:
    """Return cache settings (TTL in seconds); disabled deployments ignore tuning."""
    if os.getenv("CBIOPORTAL_MCP_QUERY_CACHE_ENABLED") != "1":
        return None
    ttl = int(os.getenv("CBIOPORTAL_MCP_QUERY_CACHE_TTL", "3600"))
    if ttl <= 0:
        raise ValueError("CBIOPORTAL_MCP_QUERY_CACHE_TTL must be positive")
    settings: dict[str, int | str] = {
        "use_query_cache": 1,
        "query_cache_ttl": ttl,
        "query_cache_nondeterministic_function_handling": "ignore",
    }
    return settings


def run_query(query: str, *, settings: dict[str, int | str] | None) -> str:
    """Use 0.5.0's client settings, cached executor and cancellation lifecycle.

    run_query has no settings argument. Its worker accepts a resolved client
    config, including settings that also participate in the client's cache key.
    Keep request credentials/roles and upstream read-only enforcement intact.
    Upstream serializes only columns/rows, so cache hits must be read from
    system.query_log rather than inferred from latency or cache enablement.
    """
    from mcp_clickhouse import mcp_server as upstream

    if settings is None:
        return upstream.run_query(query)

    overrides = dict(upstream._get_client_config_overrides() or {})
    overrides["settings"] = {**overrides.get("settings", {}), **settings}
    config = upstream._resolve_client_config(overrides)
    query_id = str(uuid.uuid4())
    state = upstream._register_active_query(query_id, query)
    try:
        try:
            future = upstream.QUERY_EXECUTOR.submit(upstream.execute_query, query, query_id, config)
        except Exception:
            upstream._remove_active_query(query_id, state)
            raise
        timeout = upstream.get_mcp_config().query_timeout
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            if future.cancel():
                upstream._remove_active_query(query_id, state)
            else:
                upstream._mark_active_query_cancelled(query_id)
                upstream._cancel_query_with_bounded_wait(query_id)
            raise upstream.ToolError(f"Query timed out after {timeout} seconds") from exc
    except upstream.ToolError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Unexpected error during query execution: {exc}") from exc
