"""Per-request ClickHouse settings through mcp-clickhouse's own execution path.

Row-policy mode sends the caller's study allowlist as a custom setting on every
SELECT. Rather than opening a separate client per query (which would bypass
mcp-clickhouse's client pool, query timeout and KILL QUERY cancellation), the
settings ride along on mcp-clickhouse's ``run_query``:

- a context variable holds the current request's extra settings;
- mcp-clickhouse's worker pool is wrapped so each submitted job runs in a copy
  of the submitting context (``ThreadPoolExecutor`` doesn't propagate
  contextvars on its own), so concurrent requests never see each other's
  settings;
- ``build_query_settings`` - which ``execute_query`` calls for every query - is
  wrapped to merge those extra settings in and to force ``readonly=1``.

``use_query_cache=0`` is forced the same way. One shared ClickHouse user
serves every caller, and the row policies depend on a per-request setting;
whether ClickHouse's query-cache key includes request settings varies by
version (25.8 does, verified locally), so row-policy mode never relies on it
and the startup check confirms the override took effect.

``readonly=1`` is the database-layer half of the allowlist protection: with it
set on the request, ClickHouse rejects any ``SETTINGS`` clause or ``SET`` inside
the query (error 164, custom settings included), while settings passed with the
request itself still apply. With ``readonly=2``, or a profile that marks the
setting ``CHANGEABLE_IN_READONLY``, a query could override its own allowlist
(verified on ClickHouse 24.12 and 25.8) - ``verify_allowlist_lockdown`` checks this at
startup. Relies on mcp-clickhouse internals, so the version is pinned in
pyproject.toml and ``install`` fails loudly if they change.
"""

from __future__ import annotations

import contextvars
import json
import math
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

_REQUEST_SETTINGS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cbiomcp_request_clickhouse_settings", default=None
)
_install_lock = threading.Lock()
_installed = False


class _ContextPropagatingExecutor:
    """Executor proxy running every job in a copy of the submitter's context."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def submit(self, fn, /, *args, **kwargs):
        return self._inner.submit(contextvars.copy_context().run, fn, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install() -> None:
    """Hook request settings into mcp-clickhouse's execution path (idempotent)."""
    global _installed
    with _install_lock:
        if _installed:
            return
        from clickhouse_connect import common as clickhouse_connect_common
        from mcp_clickhouse import mcp_server

        for attr in ("QUERY_EXECUTOR", "build_query_settings", "run_query", "execute_query"):
            if not hasattr(mcp_server, attr):
                raise RuntimeError(
                    f"mcp_clickhouse.mcp_server.{attr} is missing; the per-request settings "
                    "hook needs updating for this mcp-clickhouse version"
                )

        original_build = mcp_server.build_query_settings

        def build_query_settings(client) -> dict[str, str]:
            settings = original_build(client)
            extra = _REQUEST_SETTINGS.get()
            if not extra:
                return settings
            merged = dict(extra)
            merged.update(settings)
            merged["readonly"] = "1"
            merged["use_query_cache"] = "0"
            return merged

        mcp_server.build_query_settings = build_query_settings
        mcp_server.QUERY_EXECUTOR = _ContextPropagatingExecutor(mcp_server.QUERY_EXECUTOR)
        # clickhouse-connect drops settings the server doesn't list in
        # system.settings unless told to send them; SQL_* custom settings are
        # never listed. Process-wide and set once, so no lock around queries.
        clickhouse_connect_common.set_setting("invalid_setting_action", "send")
        _installed = True


def run_query_with_settings(query: str, settings: Mapping[str, str]) -> dict:
    """Run ``query`` via mcp-clickhouse with extra per-request ClickHouse settings.

    Returns mcp-clickhouse's ``{"columns": [...], "rows": [...]}`` result.
    """
    install()
    from mcp_clickhouse import mcp_server

    token = _REQUEST_SETTINGS.set(dict(settings))
    try:
        return json.loads(mcp_server.run_query(query))
    finally:
        _REQUEST_SETTINGS.reset(token)


class AllowlistLockdownError(RuntimeError):
    """ClickHouse would let a query change (or the app couldn't set) the allowlist."""


def verify_allowlist_lockdown(setting_name: str) -> None:
    """Fail unless the app can set ``setting_name`` and a query cannot.

    1. The setting sent with the request must reach the query
       (``getSetting`` round-trip) - otherwise row policies can't work (e.g.
       the runtime user's profile enforces readonly=1).
    2. A ``SETTINGS <setting_name> = '*'`` clause inside the query must be
       rejected - otherwise anything that slipped past the app-level guard
       could widen its own study access (readonly=2, or the setting marked
       CHANGEABLE_IN_READONLY in the user's profile).
    """
    probe = "__cbiomcp_lockdown_probe__"
    try:
        result = run_query_with_settings(
            f"SELECT getSetting('{setting_name}') AS v, "
            "toUInt8(getSetting('use_query_cache')) AS qc",
            {setting_name: probe},
        )
    except Exception as e:
        raise AllowlistLockdownError(
            f"ClickHouse rejected the per-request {setting_name} setting ({e}). Check that "
            "custom_settings_prefixes includes its prefix and that the runtime user's "
            "profile does not enforce readonly=1."
        ) from e
    rows = result.get("rows") or [[None, None]]
    if rows[0][0] != probe:
        raise AllowlistLockdownError(
            f"{setting_name} sent with the request did not reach the query: {result}"
        )
    if str(rows[0][1]) not in {"0", "False", "false"}:
        raise AllowlistLockdownError(
            "use_query_cache is still enabled for row-policy queries; the query cache could "
            f"serve one caller's rows to another: {result}"
        )

    try:
        run_query_with_settings(f"SELECT 1 SETTINGS {setting_name} = '*'", {setting_name: ""})
    except Exception as e:
        if clickhouse_error_code(e) == READONLY_CODE:
            return  # expected: readonly=1 forbids changing settings inside the query
        raise AllowlistLockdownError(
            f"Could not confirm ClickHouse rejects in-query {setting_name} overrides: {e}"
        ) from e
    raise AllowlistLockdownError(
        f"A query was able to override {setting_name} with its own SETTINGS clause. The "
        "runtime user must not have readonly=2 or a CHANGEABLE_IN_READONLY constraint on "
        "this setting; row policies would otherwise be bypassable."
    )


# Engines whose rows don't pass through the caller's row policies the way a
# plain table's do: dictGet() reads a dictionary's source with the
# dictionary's own rights, and live/window/materialized views run their query
# outside the reader's request.
BYPASS_ENGINES = ("MaterializedView", "Dictionary", "LiveView", "WindowView")


# Deployment parameters baked into the stored MV definition. Each is rendered
# with a sentinel, formatted by ClickHouse, then widened back to exactly the
# values sql/study-authz/render.sh accepts for it.
_REFRESH_VIEW_PARAMETERS = {
    "__CBIOMCP_APP_NAME__": ("cbiomcpsentinelapp", r"[A-Za-z0-9_.-]+"),
    "__CBIOMCP_FILTER_GROUPS_BY_APP_NAME__": ("4242424242", r"[01]"),
    "__CBIOMCP_PUBLIC_GROUP__": ("CBIOMCPSENTINELPUB", r"[A-Za-z0-9_.-]*"),
}
# What ClickHouse writes for the target's columns and the resolved definer.
# The column list in the stored header is the SELECT's output structure, so
# its types follow the source tables (cancer_study.cancer_study_id is Int64
# upstream but Int32 in some deployments and the local e2e). Names and order
# are fixed; the types carry no security weight - the body, the target table
# and its database are compared exactly - so any ClickHouse type is accepted.
_REFRESH_VIEW_TARGET_COLUMN_NAMES = (
    "email",
    "cancer_study_identifier",
    "cancer_study_id",
    "refreshed_at",
)
_CLICKHOUSE_TYPE = r"[A-Za-z][A-Za-z0-9_]*(?:\((?:[A-Za-z0-9_,' ]|\([A-Za-z0-9_,' ]*\))*\))?"
_REFRESH_VIEW_TARGET_COLUMNS = (
    r"\("
    + r",\ ".join(f"`{name}`\\ {_CLICKHOUSE_TYPE}" for name in _REFRESH_VIEW_TARGET_COLUMN_NAMES)
    + r"\)"
)
_DEFINER = r"(?:[A-Za-z0-9_.@-]+|`(?:[^`\\]|\\.|``)+`)"


def clickhouse_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def expected_refresh_view_pattern(database: str, format_query) -> re.Pattern:
    """The exact stored form of 0-user-study.sql's MV for ``database``.

    ``format_query(sql) -> str`` must be ClickHouse's own
    ``formatQuerySingleLine`` on the server being checked: system.tables
    re-formats stored DDL with that same formatter, so the body compares
    exactly - entitlement logic, sources, joins and the refreshed_at
    expression included. Only the deployment parameters (refresh interval,
    app name, app-name filter, PUBLIC group), the definer user and the
    target column list ClickHouse adds to the header may differ.
    """
    from cbioportal_mcp.authentication.user_study_sql import refresh_view_statement

    # ClickHouse normalizes the interval into larger units when it formats
    # (60 MINUTE -> 1 HOUR), so a fixed value is rendered and that clause is
    # widened to any normalized interval below.
    statement = (
        refresh_view_statement()
        .replace("__CLICKHOUSE_DATABASE__", database)
        .replace("__CBIOMCP_REFRESH_MINUTES__", "5")
    )
    for placeholder, (sentinel, _regex) in _REFRESH_VIEW_PARAMETERS.items():
        statement = statement.replace(placeholder, sentinel)
    formatted = format_query(statement)
    pattern = re.escape(formatted)
    for sentinel, regex in _REFRESH_VIEW_PARAMETERS.values():
        escaped = re.escape(sentinel)
        if escaped not in pattern:
            raise ValueError(f"formatter dropped parameter sentinel {sentinel}")
        pattern = pattern.replace(escaped, regex)
    interval = re.escape("REFRESH EVERY 5 MINUTE ")
    if interval not in pattern:
        raise ValueError("unexpected formatting of the REFRESH clause")
    pattern = pattern.replace(
        interval, r"REFRESH\ EVERY(?:\ \d+\ (?:WEEK|DAY|HOUR|MINUTE|SECOND))+\ ", 1
    )
    definer = re.escape("DEFINER = CURRENT_USER")
    if definer not in pattern:
        raise ValueError("unexpected formatting of DEFINER = CURRENT_USER")
    pattern = pattern.replace(definer, rf"DEFINER\ =\ {_DEFINER}", 1)
    target = re.escape(f"TO {database}.user_study ")
    if target not in pattern:
        raise ValueError("unexpected formatting of the TO target")
    pattern = pattern.replace(target, f"{target}(?:{_REFRESH_VIEW_TARGET_COLUMNS}\\ )?", 1)
    return re.compile(pattern)


def _is_allowed_refresh_view(name: str, create_query: str, database: str, format_query) -> bool:
    """True only if the stored definition is exactly 0-user-study.sql's MV for ``database``.

    No heuristics: any change to the body (another source, a comma or cross
    join, a different refreshed_at expression, other entitlement logic) or
    to the header (APPEND, another target or database, another SQL SECURITY)
    fails the comparison. If the formatter can't be run, the answer is no
    (fail closed). Reading the MV object itself is caught by the
    database-wide default-deny.
    """
    from cbioportal_mcp.authentication.row_policy_catalog import USER_STUDY_REFRESH_VIEW

    if name != USER_STUDY_REFRESH_VIEW or not database:
        return False
    try:
        pattern = expected_refresh_view_pattern(database, format_query)
    except Exception:
        return False
    return pattern.fullmatch(create_query.strip()) is not None


def unsafe_database_objects(
    setting_name: str, *, allow_user_study_refresh: bool = False
) -> list[tuple[str, str]]:
    """(object, problem) for every view not explicitly SQL SECURITY INVOKER and
    every materialized view / dictionary / live or window view in the database.

    ``allow_user_study_refresh`` (authorities mode) exempts only the
    refreshable ``user_study_refresh`` MV; any other materialized view, or
    that name with a different shape, is still reported.

    A DEFINER view reads as its definer and a NONE view runs without the
    caller's row-policy context, so either leaks every study through its USING 1
    pass-through policy; a view with no clause depends on the server default
    when it was created. The clause is read with the same tokenizer-based
    helper the coverage checker uses (study_access.view_sql_security).
    """
    from cbioportal_mcp.authentication.study_access import view_sql_security

    engines = ", ".join(f"'{engine}'" for engine in ("View", *BYPASS_ENGINES))
    result = run_query_with_settings(
        "SELECT name, engine, create_table_query FROM system.tables "
        f"WHERE database = currentDatabase() AND engine IN ({engines}) ORDER BY name",
        {setting_name: ""},
    )
    problems = []
    database = None

    def format_query(sql: str) -> str:
        result = run_query_with_settings(
            f"SELECT formatQuerySingleLine({clickhouse_string_literal(sql)})", {setting_name: ""}
        )
        return result["rows"][0][0]

    for name, engine, create_query in result.get("rows", []):
        if engine != "View":
            if allow_user_study_refresh and engine == "MaterializedView" and database is None:
                database = run_query_with_settings("SELECT currentDatabase()", {setting_name: ""})[
                    "rows"
                ][0][0]
            if (
                allow_user_study_refresh
                and engine == "MaterializedView"
                and _is_allowed_refresh_view(name, create_query or "", database or "", format_query)
            ):
                continue
            problems.append((name, engine))
            continue
        security = view_sql_security(create_query or "")
        if security != "INVOKER":
            problems.append((name, f"SQL SECURITY {security or 'unset'}"))
    return problems


# Only this error code counts as "the role can't do that". Anything else
# (unknown cluster, connection refused, syntax error, missing table) proves
# nothing about privileges and makes the check fail as inconclusive.
ACCESS_DENIED_CODE = 497
READONLY_CODE = 164
# A table function the server doesn't have (not compiled in, or removed in a
# later release) can't be used either - as safe as denied, and not a reason
# to refuse to start.
UNKNOWN_FUNCTION_CODE = 46
# Where the numeric code sits, anchored at the start of the error text so a
# code (or "ACCESS_DENIED") mentioned later in the message can't count:
# clickhouse-connect's own "HTTPDriver for <url> received ClickHouse error
# code N" prefix, optionally wrapped by mcp-clickhouse's "Query execution
# failed: ", or a server message starting "Code: N.".
_ERROR_CODE_PREFIX = re.compile(
    r"\A\s*(?:Query execution failed:\s*)?"
    r"(?:HTTPDriver for \S+ received ClickHouse error code (?P<driver>\d+)\b"
    r"|Code:\s*(?P<server>\d+)\.)"
)


def clickhouse_error_code(error: BaseException | str) -> int | None:
    """The numeric ClickHouse error code of ``error``, or None if it has none.

    Uses a structured ``code`` attribute when the exception carries one
    (clickhouse-connect 0.x exceptions don't, and mcp-clickhouse re-raises
    them as text), else the leading code in the message - never a code that
    merely appears somewhere in it.
    """
    code = getattr(error, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    match = _ERROR_CODE_PREFIX.match(str(error))
    if not match:
        return None
    return int(match.group("driver") or match.group("server"))


def is_access_denied(error: BaseException | str) -> bool:
    """True only when the error's own code is 497 (ACCESS_DENIED)."""
    return clickhouse_error_code(error) == ACCESS_DENIED_CODE


# Checked in addition to the grant list below, because some access is
# implicit and never shows up in SHOW GRANTS (system tables when the server's
# select_from_system_db_requires_grant is off). Each probe targets something
# that exists on every server, so the only acceptable outcome is error 497
# (verified on 25.8 for a role holding only SELECT ON <db>.*). url()/file()
# and the external-source table functions reach outside the row-policy
# boundary (READ ON URL / MYSQL / POSTGRES / S3 / ... grants); system.processes
# / system.query_log expose other requests' SQL, and with it their allowlists
# or identities. Arguments are well-formed so the access check is what fails
# (a malformed s3() URL fails on parsing first, as code 36).
RESTRICTED_ROLE_PROBES = {
    "url()": "SELECT * FROM url('http://127.0.0.1:1/', 'TSV', 'x String') LIMIT 0",
    "remote()": "SELECT 1 FROM remote('127.0.0.1', system.one) LIMIT 0",
    "remoteSecure()": "SELECT 1 FROM remoteSecure('127.0.0.1', system.one) LIMIT 0",
    "cluster()": "SELECT 1 FROM cluster('default', system.one) LIMIT 0",
    "file()": "SELECT * FROM file('cbiomcp_probe.csv', 'CSV', 'x String') LIMIT 0",
    "input()": "SELECT * FROM input('x String') LIMIT 0",
    "mysql()": "SELECT * FROM mysql('127.0.0.1:1', 'db', 't', 'u', 'p') LIMIT 0",
    "postgresql()": "SELECT * FROM postgresql('127.0.0.1:1', 'db', 't', 'u', 'p') LIMIT 0",
    "s3()": "SELECT * FROM s3('http://127.0.0.1:1/cbiomcp-probe/k.csv', 'CSV', 'x String') "
    "LIMIT 0",
    "hdfs()": "SELECT * FROM hdfs('hdfs://127.0.0.1:1/k.csv', 'CSV', 'x String') LIMIT 0",
    "azureBlobStorage()": "SELECT * FROM azureBlobStorage('http://127.0.0.1:1/devstoreaccount1', "
    "'c', 'k.csv', 'CSV', 'x String') LIMIT 0",
    "mongodb()": "SELECT * FROM mongodb('127.0.0.1:1', 'db', 'c', 'u', 'p', 'x String') LIMIT 0",
    "redis()": "SELECT * FROM redis('127.0.0.1:1', 'k', 'k String, v String') LIMIT 0",
    "sqlite()": "SELECT * FROM sqlite('cbiomcp_probe.db', 't') LIMIT 0",
    "odbc()": "SELECT * FROM odbc('DSN=cbiomcp_probe', 't') LIMIT 0",
    "jdbc()": "SELECT * FROM jdbc('jdbc:cbiomcp_probe', 't') LIMIT 0",
    "system.processes": "SELECT 1 FROM system.processes LIMIT 0",
    "system.dictionaries": "SELECT 1 FROM system.dictionaries LIMIT 0",
    "system.row_policies": "SELECT 1 FROM system.row_policies LIMIT 0",
}


def probe_outcomes(setting_name: str) -> dict[str, str]:
    """Probe name -> "denied", "unavailable", "ALLOWED" or "inconclusive: <error>"."""
    outcomes = {}
    for name, query in RESTRICTED_ROLE_PROBES.items():
        try:
            run_query_with_settings(query, {setting_name: ""})
        except Exception as e:
            # Classify the exception itself (a structured .code wins over its
            # text); the message is only for the diagnostic below.
            message = str(e)
            if is_access_denied(e):
                outcomes[name] = "denied"
            elif name.endswith("()") and clickhouse_error_code(e) == UNKNOWN_FUNCTION_CODE:
                outcomes[name] = "unavailable"
            else:
                first_line = next((line for line in message.splitlines() if line.strip()), "")
                outcomes[name] = f"inconclusive: {first_line[:160]}"
            continue
        outcomes[name] = "ALLOWED"
    return outcomes


# The grantee in SHOW GRANTS output: a bare name or a backtick-quoted one
# (which may contain spaces, escaped backticks or backslashes). Anything after
# it - WITH GRANT OPTION, a second grantee - makes the line not match.
_PRINCIPAL = r"(?:[A-Za-z0-9_][A-Za-z0-9_.@-]*|`(?:[^`\\]|\\.|``)+`)"


def extra_grants(setting_name: str) -> list[str]:
    """Effective grants beyond ``SELECT ON <current db>.*`` (SHOW GRANTS FINAL).

    FINAL folds in every granted role, so this is what the runtime user can
    actually do. Any other GRANT - a table function, dictGet, system tables,
    another database, WITH GRANT OPTION - is reported.
    """
    database = run_query_with_settings("SELECT currentDatabase()", {setting_name: ""})
    database = database["rows"][0][0]
    rows = run_query_with_settings("SHOW GRANTS FINAL", {setting_name: ""}).get("rows", [])
    quoted_db = "`" + database.replace("\\", "\\\\").replace("`", "\\`") + "`"
    allowed = re.compile(
        rf"^GRANT SELECT ON (?:{re.escape(database)}|{re.escape(quoted_db)})\.\* "
        rf"TO {_PRINCIPAL}$"
    )
    return [
        line for (line, *_rest) in rows if line.startswith("GRANT ") and not allowed.match(line)
    ]


def verify_restricted_surface(setting_name: str, *, identity_mode: bool = False) -> None:
    """Fail on unsafe views/objects or a runtime role broader than SELECT ON <db>.*."""
    problems = []
    objects = unsafe_database_objects(setting_name, allow_user_study_refresh=identity_mode)
    if objects:
        problems.append(
            "unsafe objects in the database (views must be SQL SECURITY INVOKER - "
            "re-apply sql/study-authz/0-row-policies.sql; no materialized views or "
            "dictionaries): " + ", ".join(f"{name} ({problem})" for name, problem in objects)
        )
    try:
        grants = extra_grants(setting_name)
    except Exception as e:
        problems.append(f"could not read the runtime user's effective grants: {e}")
    else:
        if grants:
            problems.append(
                "the runtime user has grants beyond SELECT ON <db>.*: " + "; ".join(grants)
            )
    for name, outcome in probe_outcomes(setting_name).items():
        if outcome == "ALLOWED":
            problems.append(
                f"the runtime role can use {name} - keep select_from_system_db_requires_grant "
                "enabled and grant only SELECT ON <db>.*"
            )
        elif outcome not in {"denied", "unavailable"}:
            problems.append(f"could not confirm {name} is denied (expected error 497): {outcome}")
    if problems:
        raise AllowlistLockdownError("; ".join(problems))


# Health of the periodic re-check. Startup already refuses an unsafe
# database; the monitor catches it becoming unsafe afterwards (a view
# re-created as DEFINER/NONE, a grant added) without a restart. The query gate
# fails closed unless the monitor is running and has completed a check
# recently, so a dead or stuck monitor can't leave queries flowing unchecked.
_surface_problem: str | None = None
_surface_lock = threading.Lock()
_monitor_thread: threading.Thread | None = None
_monitor_interval: float | None = None
_last_check_at: float | None = None  # time.monotonic() of the last completed check

MIN_RECHECK_SECONDS = 1.0
MAX_RECHECK_SECONDS = 86400.0
# A check older than this many intervals means the monitor is dead or stuck.
STALE_AFTER_INTERVALS = 3


def parse_recheck_interval(raw: str | None) -> float:
    """Validate CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS for row-policy mode.

    Must be a finite number of seconds in [1, 86400]. NaN/inf, zero, negative
    and unparseable values are rejected rather than quietly disabling the
    monitor (time.sleep(nan) would kill its thread).
    """
    value = "300" if raw is None or not raw.strip() else raw.strip()
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(f"must be a number of seconds, got {value!r}") from None
    if not math.isfinite(seconds) or not MIN_RECHECK_SECONDS <= seconds <= MAX_RECHECK_SECONDS:
        raise ValueError(
            f"must be between {MIN_RECHECK_SECONDS:g} and {MAX_RECHECK_SECONDS:g} seconds, "
            f"got {value!r}"
        )
    return seconds


def current_surface_problem() -> str | None:
    """Why study-data queries must be refused right now, or None."""
    with _surface_lock:
        problem = _surface_problem
        thread, interval, last = _monitor_thread, _monitor_interval, _last_check_at
    if problem:
        return problem
    if thread is None or interval is None or last is None:
        return "the row-policy safety re-check has not been started"
    if not thread.is_alive():
        return "the row-policy safety re-check thread has stopped"
    age = time.monotonic() - last
    if age > STALE_AFTER_INTERVALS * interval:
        return f"the last row-policy safety check finished {age:.0f}s ago (stale)"
    return None


def recheck_surface(setting_name: str, *, identity_mode: bool = False) -> None:
    """Re-run verify_restricted_surface and record the outcome (fail closed)."""
    global _surface_problem, _last_check_at
    try:
        verify_restricted_surface(setting_name, identity_mode=identity_mode)
        problem = None
    except AllowlistLockdownError as e:
        problem = str(e)
    except Exception as e:  # can't check -> can't vouch for the database
        problem = f"re-check could not run: {e}"
    with _surface_lock:
        _surface_problem = problem
        _last_check_at = time.monotonic()


def stop_surface_monitor(thread: threading.Thread, timeout: float = 5.0) -> None:
    """Stop a thread started by start_surface_monitor (tests; the server never stops it).

    A monitor left running after a test keeps issuing real re-check queries
    through whatever ClickHouse client a later test has mocked.
    """
    stop = getattr(thread, "stop_event", None)
    if stop is not None:
        stop.set()
    thread.join(timeout)


def start_surface_monitor(
    setting_name: str, interval_seconds: float, *, identity_mode: bool = False
) -> threading.Thread:
    """Re-check every ``interval_seconds`` on a daemon thread.

    Call right after a successful startup check, which counts as the first
    completed check. Any exception escaping a cycle marks the state unhealthy
    instead of ending the thread silently.
    """
    global _monitor_thread, _monitor_interval, _last_check_at, _surface_problem
    interval = parse_recheck_interval(str(interval_seconds))

    # Set only by stop_surface_monitor (tests); otherwise an interruptible sleep.
    stop = threading.Event()
    wait = stop.wait

    def loop() -> None:
        global _surface_problem
        while not stop.is_set():
            try:
                if wait(interval):
                    return
                recheck_surface(setting_name, identity_mode=identity_mode)
            except BaseException as e:  # noqa: B036 - must never die silently
                with _surface_lock:
                    _surface_problem = f"row-policy safety re-check crashed: {e!r}"
                if not isinstance(e, Exception):
                    raise
                wait(MIN_RECHECK_SECONDS)  # don't spin if every cycle fails

    thread = threading.Thread(target=loop, name="cbiomcp-surface-recheck", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    with _surface_lock:
        _monitor_thread, _monitor_interval = thread, interval
        _last_check_at = time.monotonic()
        _surface_problem = None
    thread.start()
    return thread
