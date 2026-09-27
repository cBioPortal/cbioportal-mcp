"""Opt-in ClickHouse query cache for standard frequency / top-gene SELECTs.

Only ``CBIOPORTAL_MCP_QUERY_CACHE_ENABLED=1`` (exactly the string ``1``) enables
the pilot; any other value, including ``true``, leaves it off. When on, a SELECT
is cached only if its intent is known to be a repeatable frequency or top-gene
lookup:

- internal call sites whose ``query_label`` is in ``CACHEABLE_LABELS``, and
- agent-written SQL (``clickhouse_run_select_query``) whose every FROM/JOIN
  source is a standard parameterized view in ``CACHEABLE_VIEWS`` (or a
  subquery / CTE built from them) and that does not reference ``system.*``.
  Comments and quoted text are scrubbed before matching; anything the check
  can't account for runs uncached.

Everything else (list_studies, other study_guide sections, arbitrary SQL, the
background studies refresh) runs exactly as before.

Settings are passed with mcp-clickhouse 0.5.0's supported request-context
overrides (``clickhouse_client_config_overrides`` state), so upstream
``run_query`` keeps its credentials/roles, read-only enforcement, timeouts and
cancellation. Without an active FastMCP request context there is nowhere to
put the overrides, so the query runs uncached.

Server preconditions: ClickHouse 24.4+, and a DB user profile that lets the
MCP change the three cache settings: ``readonly=2`` (recommended), or
``readonly=1`` with ``CHANGEABLE_IN_READONLY`` constraints on
``use_query_cache``, ``query_cache_ttl`` and
``query_cache_nondeterministic_function_handling``. With a plain
``readonly=1`` profile the server refuses the settings; the pilot then logs
once, counts ``db_query.cache_disabled`` and turns itself off for the process.

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
CACHEABLE_VIEWS = frozenset(
    {
        "gene_mutation_frequency_by_cancer_type",
        "gene_mutation_frequency_in_study",
        "gene_mutation_frequency_in_studies",
        "gene_alteration_frequency_by_cancer_type",
        "top_mutated_genes_in_cohort",
        "top_mutated_genes_in_study",
        "top_cna_genes_in_study",
        "top_sv_genes_in_study",
    }
)
CACHE_SETTING_NAMES = frozenset(
    {"use_query_cache", "query_cache_ttl", "query_cache_nondeterministic_function_handling"}
)

# --- SQL scrubbing -----------------------------------------------------------
# Same token shapes as mcp-clickhouse's _strip_comments_and_quoted_text, kept
# as a local copy so this module doesn't depend on a private upstream helper.
# Comments and string literals are blanked; quoted identifiers are unquoted so
# `system`.tables or "top_mutated_genes_in_cohort"(...) are seen as written.
_SQL_TOKENS = re.compile(
    r"""
      (?P<string>'(?:\\.|''|[^'\\])*')
    | "(?P<dquoted>(?:\\.|""|[^"\\])*)"
    | `(?P<bquoted>(?:\\.|``|[^`\\])*)`
    | (?P<dollar>\$(?P<tag>\w*)\$.*?\$(?P=tag)\$)
    | (?P<comment>--[^\n]*|\#[^\n]*|/\*.*?\*/)
    """,
    re.VERBOSE | re.DOTALL,
)
# Unterminated quotes/comments survive scrubbing; treat them as unparseable.
_LEFTOVER_QUOTE_OR_COMMENT = re.compile(r"['\"`$#]|--|/\*")


def _scrub_sql(query: str) -> str:
    def replace(match: re.Match) -> str:
        ident = match.group("dquoted")
        if ident is None:
            ident = match.group("bquoted")
        if ident is None:
            return " "
        # Keep word-ish identifiers verbatim; anything else can't be a view or
        # the system database, so make it an identifier that matches neither.
        return f" {ident} " if re.fullmatch(r"\w+", ident) else " _quoted_ident_ "

    return _SQL_TOKENS.sub(replace, query)


_SYSTEM_REF = re.compile(r"\bsystem\b\s*[`\"]?\s*\.", re.IGNORECASE)
# Any assignment to a query cache setting, e.g. `SETTINGS use_query_cache = 1`.
_QUERY_CACHE_ASSIGNMENT = re.compile(r"\b\w*query_cache\w*\s*=", re.IGNORECASE)
_SOURCE_KEYWORD = re.compile(r"\b(?:FROM|JOIN)\b", re.IGNORECASE)
# Other ways ClickHouse reads a table outside FROM/JOIN: `x IN table_name`,
# dictionaries, Join-engine tables.
_OTHER_TABLE_READS = re.compile(
    r"\bIN\s+(?!\()[A-Za-z_]|\b(?:dict\w*|joinGet\w*)\s*\(",
    re.IGNORECASE,
)
_CTE_FIRST = re.compile(r"\bWITH\s+(\w+)\s+AS\s*\(", re.IGNORECASE)
_CTE_NEXT = re.compile(r"\)\s*,\s*(\w+)\s+AS\s*\(", re.IGNORECASE)
_IDENT = re.compile(r"\s*(\w+)")
_ALIAS_STOPWORDS = frozenset(
    {
        "where", "group", "order", "limit", "having", "settings", "format", "union",
        "intersect", "except", "left", "right", "inner", "outer", "full", "cross",
        "any", "all", "asof", "semi", "anti", "global", "join", "array", "on",
        "using", "final", "sample", "prewhere", "window", "qualify", "offset",
    }
)  # fmt: skip


def _matching_paren(sql: str, open_index: int) -> int | None:
    depth = 0
    for i in range(open_index, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _source_end_is_single(sql: str, end: int) -> bool:
    """After a FROM/JOIN source ending at ``end``, skip an optional alias and
    require that no comma-joined second source follows."""
    rest = sql[end:]
    match = _IDENT.match(rest)
    if match and match.group(1).lower() == "as":
        rest = rest[match.end() :]
        match = _IDENT.match(rest)
    if match and match.group(1).lower() not in _ALIAS_STOPWORDS:
        rest = rest[match.end() :]
    return not rest.lstrip().startswith(",")


def _only_standard_view_sources(sql: str) -> bool:
    """True when every FROM/JOIN source is an allowlisted view call, a
    parenthesized subquery, or a CTE name, and at least one view is called.
    Errs toward False on anything it doesn't recognize."""
    ctes = {name.lower() for name in _CTE_FIRST.findall(sql) + _CTE_NEXT.findall(sql)}
    saw_view = False
    for keyword in _SOURCE_KEYWORD.finditer(sql):
        pos = keyword.end()
        while pos < len(sql) and sql[pos].isspace():
            pos += 1
        if sql.startswith("(", pos):
            end = _matching_paren(sql, pos)
            if end is None:
                return False
            source_end = end + 1
        else:
            match = re.compile(r"(\w+)(\s*\.)?\s*(\()?").match(sql, pos)
            if match is None or match.group(2):
                return False  # database-qualified or not an identifier
            name = match.group(1).lower()
            if match.group(3):
                if name not in CACHEABLE_VIEWS:
                    return False
                end = _matching_paren(sql, match.end() - 1)
                if end is None:
                    return False
                saw_view = True
                source_end = end + 1
            elif name in ctes:
                source_end = match.end()
            else:
                return False
        if not _source_end_is_single(sql, source_end):
            return False
    return saw_view


def _is_cacheable(query_label: str, query: str) -> bool:
    if query_label in CACHEABLE_LABELS:
        return True
    if query_label != LLM_QUERY_LABEL:
        return False
    sql = _scrub_sql(query)
    if _LEFTOVER_QUOTE_OR_COMMENT.search(sql) or _SYSTEM_REF.search(sql):
        return False
    if _OTHER_TABLE_READS.search(sql):
        return False
    return _only_standard_view_sources(sql)


# --- Configuration -----------------------------------------------------------

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


def query_cache_settings(query_label: str, query: str) -> dict[str, Any] | None:
    """Return ClickHouse cache settings for this SELECT, or None to run uncached.

    With the pilot on, agent SQL that sets query_cache* itself is rejected so a
    caller cannot bypass the TTL cap or cache arbitrary queries.
    """
    ttl = load_config()
    # Pilot off (or self-disabled): agent SQL passes through untouched, including
    # any SETTINGS use_query_cache it carries. That is the pre-pilot behavior,
    # and the DB profile constraints (query_cache_ttl max, share_between_users
    # readonly) still bound it.
    if ttl is None or _server_rejected:
        return None
    if query_label == LLM_QUERY_LABEL and _QUERY_CACHE_ASSIGNMENT.search(_scrub_sql(query)):
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


# --- Execution ---------------------------------------------------------------

# Messages ClickHouse / clickhouse-connect use when a setting is unknown to the
# server or not changeable by the profile. Each captures the setting's name, so
# only a refusal of one of *our* settings disables the pilot; a model mistake
# like `SETTINGS readonly = 0` is an ordinary query error.
_REJECTED_SETTING = re.compile(
    r"Setting '?(?P<a>\w+)'? is unknown or readonly"
    r"|Unknown setting '?(?P<b>\w+)'?"
    r"|Cannot modify '(?P<c>\w+)' setting"
    r"|Setting '?(?P<d>\w+)'? (?:shouldn't|should not) be",
    re.IGNORECASE,
)


def _rejects_cache_setting(exc: Exception) -> bool:
    for match in _REJECTED_SETTING.finditer(str(exc)):
        name = next(group for group in match.groups() if group)
        if name.lower() in CACHE_SETTING_NAMES:
            return True
    return False


# The overrides live on the request's Context, which a copied contextvars
# context shares across threads. One lock per Context (kept in its own state,
# so it lives exactly as long as the request) serializes set -> run -> restore
# within a request: concurrent queries can't see each other's temporary
# settings or restore a stale snapshot, and separate requests don't contend.
_LOCK_STATE_KEY = "cbioportal_mcp.query_cache_lock"
_lock_creation_guard = threading.Lock()


def _lock_for(ctx) -> threading.Lock:
    with _lock_creation_guard:
        lock = ctx.get_state(_LOCK_STATE_KEY)
        if lock is None:
            lock = threading.Lock()
            ctx.set_state(_LOCK_STATE_KEY, lock)
        return lock


def _disable_pilot(exc: Exception) -> None:
    global _server_rejected
    from cbioportal_mcp.telemetry import emit_query_cache_disabled

    with _config_lock:
        if _server_rejected:
            return
        _server_rejected = True
    logger.warning("ClickHouse rejected query cache settings; disabling the pilot: %s", exc)
    emit_query_cache_disabled()


def run_query(query: str, *, settings: dict[str, Any] | None) -> tuple[str, bool]:
    """Run ``query`` through upstream ``run_query``, adding ``settings`` if given.

    Returns ``(result_json, cache_settings_applied)``. The second value is False
    when the server refused the settings and an uncached retry produced the
    result. The settings are merged into a copy of the request's client-config
    overrides for the duration of the call and the original state is restored
    afterwards, so later queries in the same request are unaffected.
    """
    from mcp_clickhouse import mcp_server as upstream

    # Pilot off: a straight pass-through, with no lock and no context lookup.
    ctx = _request_context() if settings is not None or load_config() is not None else None
    if ctx is None:
        return upstream.run_query(query), False
    if settings is None:
        # Uncached, but another thread of this request may be mid-way through a
        # cached query; wait so upstream doesn't resolve its temporary overrides.
        with _lock_for(ctx):
            return upstream.run_query(query), False

    key = upstream.CLIENT_CONFIG_OVERRIDES_KEY
    with _lock_for(ctx):
        original = ctx.get_state(key)
        overrides = dict(original or {})
        overrides["settings"] = {**(overrides.get("settings") or {}), **settings}
        ctx.set_state(key, overrides)
        try:
            return upstream.run_query(query), True
        except Exception as exc:
            if not _rejects_cache_setting(exc):
                raise
            _disable_pilot(exc)
        finally:
            ctx.set_state(key, original)
        # The server refused the settings before running the query; retry
        # uncached (still under the lock, with the original overrides).
        return upstream.run_query(query), False
