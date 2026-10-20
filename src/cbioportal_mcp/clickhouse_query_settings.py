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
import threading
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
        message = str(e)
        if "READONLY" in message or "Code: 164" in message or "readonly mode" in message:
            return  # expected: readonly=1 forbids changing settings inside the query
        raise AllowlistLockdownError(
            f"Could not confirm ClickHouse rejects in-query {setting_name} overrides: {e}"
        ) from e
    raise AllowlistLockdownError(
        f"A query was able to override {setting_name} with its own SETTINGS clause. The "
        "runtime user must not have readonly=2 or a CHANGEABLE_IN_READONLY constraint on "
        "this setting; row policies would otherwise be bypassable."
    )
