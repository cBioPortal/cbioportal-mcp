#!/usr/bin/env python3
"""
Permission checks for cBioPortal MCP ClickHouse user.

On startup we verify that the configured ClickHouse user:

1. Has the minimal required privileges to do its job:
   - SELECT on the application database (config.mcp_database.*)
   - Schema discovery uses SHOW TABLES and DESCRIBE TABLE, which
     only require SELECT on the target database (no system.* access needed).

2. Does NOT have excessive privileges:
   - No INSERT / UPDATE / DELETE / DDL / admin privileges on *.*.

3. If the database has projections (sql/9-projections.sql), has
   PROJECTION_SAFE_SETTINGS in effect and cannot turn them off from a query.

If checks fail, we raise PermissionError so the application can fail fast.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from cbioportal_mcp.env import McpConfig
from mcp_clickhouse.mcp_server import run_query
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

FORBIDDEN_PRIVS = {
    "INSERT",
    "ALTER",
    "CREATE",
    "DROP",
    "TRUNCATE",
    "OPTIMIZE",
    "ACCESS MANAGEMENT",
    "SYSTEM",
    "ALL",
}

# Settings that keep query results identical whether or not a projection
# serves the query. With ClickHouse's default optimize_use_implicit_projections
# = 1, a count() whose WHERE the base primary key can answer over-counts on a
# table with a normal projection: the implicit exact-count path counts the
# granules the base key proves fully matching, and the projection read counts
# them again (reproduced on 24.8.14 through 26.9.3; see sql/9-projections.sql).
PROJECTION_SAFE_SETTINGS = {"optimize_use_implicit_projections": "0"}


def _check_grant(priv: str, scope: str) -> bool:
    """
    Use CHECK GRANT <priv> ON <scope> to see if the current user has a privilege.

    Valid scopes include:
      - "<db>.*"
      - "*.*"
      - "<db>.table[*]" (not used here, but legal)

    Returns True iff result == 1.

    Important: CHECK GRANT may return a row with no column names, so we read
    the first value from rows[0][0] rather than relying on column metadata.
    """
    scope = scope.strip()
    if scope == "*":
        scope = "*.*"

    try:
        raw = json.loads(run_query(f"CHECK GRANT {priv} ON {scope}"))
    except ToolError as e:
        logger.warning(
            "CHECK GRANT %s ON %s failed (treating as not granted): %s",
            priv,
            scope,
            e,
        )
        return False
    rows = raw.get("rows") or []
    if not rows:
        # No rows means "no" or an unexpected shape; treat as not granted.
        return False

    row0 = rows[0]
    if not row0:
        return False

    val = row0[0]
    try:
        return int(val) == 1
    except Exception:
        return False


def _forbidden_privs_present() -> List[str]:
    """
    Returns a list of forbidden privileges for which CHECK GRANT ... ON *.* is true.
    """
    bad: List[str] = []
    for p in FORBIDDEN_PRIVS:
        if _check_grant(p, "*.*"):
            bad.append(p)
    return bad


def _connection_database() -> str | None:
    """The database agent SQL runs against: currentDatabase() on the MCP's own
    connection, which can differ from config.mcp_database when
    CLICKHOUSE_DATABASE is unset. None if it can't be read or is empty."""
    try:
        raw = json.loads(run_query("SELECT currentDatabase()"))
    except ToolError as e:
        logger.warning("Could not read currentDatabase(): %s", e)
        return None
    value = (raw.get("rows") or [[None]])[0][0]
    if value is None:
        return None
    return str(value).strip() or None


def _database_has_projections(database: str) -> bool:
    """True if any table in `database` defines a projection.

    If system.tables can't be read, assume projections exist so the settings
    check below still runs (fail closed).
    """
    literal = database.replace("\\", "\\\\").replace("'", "\\'")
    try:
        raw = json.loads(
            run_query(
                "SELECT count() FROM system.tables "
                f"WHERE database = '{literal}' AND create_table_query LIKE '%PROJECTION%'"
            )
        )
    except ToolError as e:
        logger.warning("Could not list projections (assuming some exist): %s", e)
        return True
    rows = raw.get("rows") or [[1]]
    return int(rows[0][0]) > 0


def _setting_value(value: Any) -> str:
    """Normalize a getSetting() result: booleans come back as True/False."""
    text = str(value).strip().lower()
    return {"false": "0", "true": "1"}.get(text, text)


def _projection_settings_problems() -> List[str]:
    """Describe how PROJECTION_SAFE_SETTINGS are not in force, or [] if they are.

    Each setting must already hold its safe value for this user, and a query's
    own SETTINGS clause must not be able to change it (readonly = 1 profile, or
    a CONST constraint). Both are probed through run_query, the same path agent
    SQL takes, so per-request settings the MCP sends are accounted for.
    """
    problems = []
    for name, safe in PROJECTION_SAFE_SETTINGS.items():
        try:
            raw = json.loads(run_query(f"SELECT getSetting('{name}')"))
            value = (raw.get("rows") or [[None]])[0][0]
        except ToolError as e:
            problems.append(f"{name}: cannot read it ({e})")
            continue
        if _setting_value(value) != safe:
            problems.append(f"{name} = {value}, must be {safe}")
            continue
        flipped = "1" if safe == "0" else "0"
        try:
            run_query(f"SELECT getSetting('{name}') SETTINGS {name} = {flipped}")
        except ToolError:
            continue  # the override was refused, as required
        problems.append(f"{name} can be changed by a query's SETTINGS clause")
    return problems


def ensure_projection_safe_settings(config: McpConfig) -> None:
    """Refuse to serve a database with projections unless results stay exact.

    Raises PermissionError with the profile change that fixes it, or when the
    connection's database can't be determined: without it there is no way to
    know whether the database agent SQL runs against has projections, so the
    server must not start even if the settings look safe.
    """
    database = _connection_database()
    if database is None:
        raise PermissionError(
            "Settings check failed: could not determine the ClickHouse connection's "
            "current database (SELECT currentDatabase() failed or returned nothing), "
            "so the projection check cannot run. Check CLICKHOUSE_DATABASE and that "
            f"user '{config.mcp_user}' can connect."
        )
    if database != config.mcp_database:
        logger.warning(
            "⚠️ The ClickHouse connection's current database is '%s' but the MCP is "
            "configured for '%s' (CLICKHOUSE_DATABASE). Agent SQL runs against '%s'; "
            "the projection check below inspects that database. Set CLICKHOUSE_DATABASE "
            "so both agree.",
            database,
            config.mcp_database,
            database,
        )
    if not _database_has_projections(database):
        return
    problems = _projection_settings_problems()
    if not problems:
        logger.info(
            "✅ Projection-safe settings are pinned for user '%s' on DB '%s'.",
            config.mcp_user,
            database,
        )
        return
    pins = ", ".join(f"{k} = {v} CONST" for k, v in PROJECTION_SAFE_SETTINGS.items())
    raise PermissionError(
        f"Settings check failed: database '{database}' has projections, and with ClickHouse's "
        "default settings some count() queries over-count on projected tables.\n"
        + "".join(f"- {p}\n" for p in problems)
        + "Pin the settings in the MCP user's profile, e.g.:\n"
        f"  ALTER USER {config.mcp_user} SETTINGS {pins};\n"
        'or drop the projections (see sql/README.md, "Projections").'
    )


def ensure_db_permissions(config: McpConfig) -> None:
    """
    Main startup gate: verify minimal and maximal privileges for the MCP DB user.

    - Minimal:
        * SELECT ON <config.mcp_database>.* must be granted.
        * Schema discovery (SHOW TABLES, DESCRIBE TABLE) is implicitly
          allowed when SELECT is granted on the database.

    - Maximal:
        * No FORBIDDEN_PRIVS may be granted on *.*.

    - Projections: see ensure_projection_safe_settings.

    Raises PermissionError if any check fails.
    """
    user = config.mcp_user
    db = config.mcp_database

    logger.info(
        "🔐 Checking ClickHouse privileges for user '%s' on DB '%s'.",
        user,
        db,
    )

    if not _check_grant("SELECT", f"{db}.*"):
        raise PermissionError(
            "Permission check failed: the MCP ClickHouse user lacks required privileges.\n"
            f"- Missing: SELECT ON {db}.* for user '{user}'.\n"
            "Grant minimally:\n"
            f"  GRANT SELECT ON {db}.* TO {user};"
        )

    bad_privs = _forbidden_privs_present()
    if bad_privs:
        raise PermissionError(
            "Permission check failed: the MCP ClickHouse user has excessive privileges.\n"
            f"- Forbidden privileges detected on *.*: {', '.join(sorted(bad_privs))}\n"
            "The MCP ClickHouse user must be strictly read-only. "
            "Revoke these permissions, e.g.:\n"
            f"  REVOKE {', '.join(sorted(bad_privs))} ON *.* FROM {user};"
        )

    ensure_projection_safe_settings(config)

    logger.info(
        "✅ ClickHouse permission checks passed for user '%s' on DB '%s'.",
        user,
        db,
    )
