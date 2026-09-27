"""Opt-in ClickHouse query cache for standard frequency / top-gene SELECTs.

Only ``CBIOPORTAL_MCP_QUERY_CACHE_ENABLED=1`` (exactly the string ``1``) enables
the pilot; any other value, including ``true``, leaves it off. When on, a SELECT
is cached only if its intent is known to be a repeatable frequency or top-gene
lookup:

- internal call sites whose ``query_label`` is in ``CACHEABLE_LABELS``, and
- agent-written SQL (``clickhouse_run_select_query``) that calls one of the
  standard parameterized views in ``CACHEABLE_VIEWS`` and does not read
  ``system.*`` tables.

Everything else (list_studies, other study_guide sections, arbitrary SQL, the
background studies refresh) runs exactly as before.

Settings are passed with mcp-clickhouse 0.5.0's supported request-context
overrides (``clickhouse_client_config_overrides`` state), so upstream
``run_query`` keeps its credentials/roles, read-only enforcement, timeouts and
cancellation. Without an active FastMCP request context there is nowhere to
put the overrides, so the query runs uncached.

Server preconditions: ClickHouse 24.4+, and a DB user profile that may change
settings (readonly 0 or 2; readonly=1 forbids them). If the server rejects the
settings anyway, the pilot logs once and turns itself off for the process.

Cache entries are not invalidated by the daily blue/green database swap; see
the PR description for the operator-side ``SYSTEM DROP QUERY CACHE`` step and
the recommended user-profile constraints.
"""

import logging
import os
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

ENABLED_ENV = "CBIOPORTAL_MCP_QUERY_CACHE_ENABLED"
TTL_ENV = "CBIOPORTAL_MCP_QUERY_CACHE_TTL"
DEFAULT_TTL_SECONDS = 3600
# Upper bound on how long a result may outlive the data it was read from. Keep
# the ClickHouse profile's query_cache_ttl max constraint equal to this.
MAX_TTL_SECONDS = 3600

LLM_QUERY_LABEL = "clickhouse_run_select_query"
CACHEABLE_LABELS = frozenset({"study_guide.top_genes"})
# Frequency / top-gene parameterized views from sql/4-mutation-frequency-views.sql.
CACHEABLE_VIEWS = (
    "gene_mutation_frequency_by_cancer_type",
    "gene_mutation_frequency_in_study",
    "gene_mutation_frequency_in_studies",
    "gene_alteration_frequency_by_cancer_type",
    "top_mutated_genes_in_cohort",
    "top_mutated_genes_in_study",
    "top_cna_genes_in_study",
    "top_sv_genes_in_study",
)
_VIEW_CALL = re.compile(r"\b(?:%s)\s*\(" % "|".join(CACHEABLE_VIEWS), re.IGNORECASE)
_SYSTEM_TABLE = re.compile(r"\bsystem\s*\.", re.IGNORECASE)
# Assignments to any query_cache setting, e.g. `SETTINGS use_query_cache = 1`.
_QUERY_CACHE_SETTING = re.compile(r"\b\w*query_cache\w*\s*=", re.IGNORECASE)
# Messages ClickHouse / clickhouse-connect use when a setting is unknown to the
# server or not changeable by the profile (readonly, constraints).
_REJECTED_SETTING = re.compile(
    r"unknown or readonly|UNKNOWN_SETTING|SETTING_CONSTRAINT_VIOLATION"
    r"|Cannot modify '\w*query_cache",
    re.IGNORECASE,
)

_config_lock = threading.Lock()
_config_loaded = False
_ttl_seconds: int | None = None
# Flipped once when the server rejects the cache settings; the pilot then stays
# off for the life of the process instead of failing every cacheable query.
_server_rejected = False


def load_config() -> int | None:
    """Read the pilot env vars once and return the TTL, or None when disabled.

    Called from server startup so a bad TTL fails fast; later calls reuse the
    validated value. TTLs above ``MAX_TTL_SECONDS`` are clamped with a warning.
    """
    global _config_loaded, _ttl_seconds
    with _config_lock:
        if _config_loaded:
            return _ttl_seconds
        ttl = None
        if os.getenv(ENABLED_ENV) == "1":
            raw = os.getenv(TTL_ENV, str(DEFAULT_TTL_SECONDS))
            try:
                ttl = int(raw)
            except ValueError:
                raise ValueError(f"{TTL_ENV} must be an integer number of seconds") from None
            if ttl <= 0:
                raise ValueError(f"{TTL_ENV} must be positive")
            if ttl > MAX_TTL_SECONDS:
                logger.warning(
                    "%s=%d exceeds the %d-second cap; using %d",
                    TTL_ENV,
                    ttl,
                    MAX_TTL_SECONDS,
                    MAX_TTL_SECONDS,
                )
                ttl = MAX_TTL_SECONDS
            logger.info("ClickHouse query cache pilot enabled (ttl=%ds)", ttl)
        _ttl_seconds = ttl
        _config_loaded = True
        return ttl


def reset_for_tests() -> None:
    global _config_loaded, _ttl_seconds, _server_rejected
    with _config_lock:
        _config_loaded = False
        _ttl_seconds = None
        _server_rejected = False


def _request_context():
    from fastmcp.server.dependencies import get_context

    try:
        return get_context()
    except RuntimeError:
        return None


def _is_cacheable(query_label: str, query: str) -> bool:
    if query_label in CACHEABLE_LABELS:
        return True
    if query_label != LLM_QUERY_LABEL:
        return False
    return bool(_VIEW_CALL.search(query)) and not _SYSTEM_TABLE.search(query)


def query_cache_settings(query_label: str, query: str) -> dict[str, Any] | None:
    """Return ClickHouse cache settings for this SELECT, or None to run uncached.

    With the pilot on, agent SQL that sets query_cache* itself is rejected so a
    caller cannot bypass the TTL cap or cache arbitrary queries.
    """
    ttl = load_config()
    if ttl is None or _server_rejected:
        return None
    if query_label == LLM_QUERY_LABEL and _QUERY_CACHE_SETTING.search(query):
        raise ValueError(
            "query_cache settings are managed by the server; remove them from the query"
        )
    if not _is_cacheable(query_label, query) or _request_context() is None:
        return None
    return {
        "use_query_cache": 1,
        "query_cache_ttl": ttl,
        "query_cache_nondeterministic_function_handling": "ignore",
    }


def run_query(query: str, *, settings: dict[str, Any] | None) -> str:
    """Run ``query`` through upstream ``run_query``, adding ``settings`` if given.

    The settings are merged into a copy of the request's client-config
    overrides for the duration of the call; the original state is restored
    afterwards so later queries in the same request are unaffected.
    """
    global _server_rejected
    from mcp_clickhouse import mcp_server as upstream

    ctx = _request_context() if settings is not None else None
    if ctx is None:
        return upstream.run_query(query)

    key = upstream.CLIENT_CONFIG_OVERRIDES_KEY
    original = ctx.get_state(key)
    overrides = dict(original or {})
    overrides["settings"] = {**(overrides.get("settings") or {}), **settings}
    ctx.set_state(key, overrides)
    try:
        return upstream.run_query(query)
    except Exception as exc:
        if not _REJECTED_SETTING.search(str(exc)):
            raise
        if not _server_rejected:
            _server_rejected = True
            logger.warning("ClickHouse rejected query cache settings; disabling the pilot: %s", exc)
    finally:
        ctx.set_state(key, original)
    # The server refused the settings before running the query; retry uncached.
    return upstream.run_query(query)
