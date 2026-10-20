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
            f"SELECT getSetting('{setting_name}') AS v", {setting_name: probe}
        )
    except Exception as e:
        raise AllowlistLockdownError(
            f"ClickHouse rejected the per-request {setting_name} setting ({e}). Check that "
            "custom_settings_prefixes includes its prefix and that the runtime user's "
            "profile does not enforce readonly=1."
        ) from e
    if result.get("rows") != [[probe]]:
        raise AllowlistLockdownError(
            f"{setting_name} sent with the request did not reach the query: {result}"
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


def unsafe_database_objects(setting_name: str) -> list[tuple[str, str]]:
    """(object, problem) for every view not explicitly SQL SECURITY INVOKER and
    every materialized view / dictionary / live or window view in the database.

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
    for name, engine, create_query in result.get("rows", []):
        if engine != "View":
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
# reach outside the row-policy boundary; system.processes / system.query_log
# expose other requests' SQL, and with it their allowlists.
RESTRICTED_ROLE_PROBES = {
    "url()": "SELECT * FROM url('http://127.0.0.1:1/', 'TSV', 'x String') LIMIT 0",
    "remote()": "SELECT 1 FROM remote('127.0.0.1', system.one) LIMIT 0",
    "file()": "SELECT * FROM file('cbiomcp_probe.csv', 'CSV', 'x String') LIMIT 0",
    "system.processes": "SELECT 1 FROM system.processes LIMIT 0",
    "system.dictionaries": "SELECT 1 FROM system.dictionaries LIMIT 0",
    "system.row_policies": "SELECT 1 FROM system.row_policies LIMIT 0",
}


def probe_outcomes(setting_name: str) -> dict[str, str]:
    """Probe name -> "denied", "ALLOWED" or "inconclusive: <error>"."""
    outcomes = {}
    for name, query in RESTRICTED_ROLE_PROBES.items():
        try:
            run_query_with_settings(query, {setting_name: ""})
        except Exception as e:
            message = str(e)
            if is_access_denied(message):
                outcomes[name] = "denied"
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


def verify_restricted_surface(setting_name: str) -> None:
    """Fail on unsafe views/objects or a runtime role broader than SELECT ON <db>.*."""
    problems = []
    objects = unsafe_database_objects(setting_name)
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
        elif outcome != "denied":
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


def recheck_surface(setting_name: str) -> None:
    """Re-run verify_restricted_surface and record the outcome (fail closed)."""
    global _surface_problem, _last_check_at
    try:
        verify_restricted_surface(setting_name)
        problem = None
    except AllowlistLockdownError as e:
        problem = str(e)
    except Exception as e:  # can't check -> can't vouch for the database
        problem = f"re-check could not run: {e}"
    with _surface_lock:
        _surface_problem = problem
        _last_check_at = time.monotonic()


def start_surface_monitor(setting_name: str, interval_seconds: float) -> threading.Thread:
    """Re-check every ``interval_seconds`` on a daemon thread.

    Call right after a successful startup check, which counts as the first
    completed check. Any exception escaping a cycle marks the state unhealthy
    instead of ending the thread silently.
    """
    global _monitor_thread, _monitor_interval, _last_check_at, _surface_problem
    interval = parse_recheck_interval(str(interval_seconds))

    wait = threading.Event().wait  # never set: an interruptible sleep

    def loop() -> None:
        global _surface_problem
        while True:
            try:
                wait(interval)
                recheck_surface(setting_name)
            except BaseException as e:  # noqa: B036 - must never die silently
                with _surface_lock:
                    _surface_problem = f"row-policy safety re-check crashed: {e!r}"
                if not isinstance(e, Exception):
                    raise
                wait(MIN_RECHECK_SECONDS)  # don't spin if every cycle fails

    thread = threading.Thread(target=loop, name="cbiomcp-surface-recheck", daemon=True)
    with _surface_lock:
        _monitor_thread, _monitor_interval = thread, interval
        _last_check_at = time.monotonic()
        _surface_problem = None
    thread.start()
    return thread
