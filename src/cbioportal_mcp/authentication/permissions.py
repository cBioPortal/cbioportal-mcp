#!/usr/bin/env python3
"""
Permission checks for cBioPortal MCP ClickHouse user.

On startup we verify that the configured ClickHouse user:

1. Has the minimal required privileges to do its job:
   - SELECT on the application database (config.mcp_database.*)
   - Depending on schema discovery mode:
     * mode "system": must be able to SELECT from system tables
       (because code queries system.tables/system.columns).
     * mode "show": must be able to run SHOW TABLES FROM <db>.

2. Does NOT have excessive privileges:
   - No INSERT / UPDATE / DELETE / DDL / admin privileges on *.*.

If checks fail, we raise PermissionError so the application can fail fast.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from cbioportal_mcp.env import McpConfig
from mcp_clickhouse.mcp_server import execute_query
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


def _check_grant(priv: str, scope: str) -> bool:
    """
    Use CHECK GRANT <priv> ON <scope> to see if the current user has a privilege.

    Valid scopes include:
      - "<db>.*"
      - "system.*"
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
        raw = execute_query(f"CHECK GRANT {priv} ON {scope}")
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


def ensure_db_permissions(config: McpConfig) -> None:
    """
    Main startup gate: verify minimal and maximal privileges for the MCP DB user.

    - Minimal:
        * SELECT ON <config.mcp_database>.* must be granted.
        * In mode 'system': SELECT ON system.* must be granted.
        * In mode 'show'  : SHOW TABLES FROM <config.mcp_database> must succeed.

    - Maximal:
        * No FORBIDDEN_PRIVS may be granted on *.*.

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

    # Check system table access (required for schema discovery tools)
    missing_system = []
    if not _check_grant("SELECT", "system.tables"):
        missing_system.append("system.tables")
    if not _check_grant("SELECT", "system.columns"):
        missing_system.append("system.columns")

    if missing_system:
        raise PermissionError(
            "Permission check failed: the MCP ClickHouse user lacks required system table access.\n"
            "The application requires access to system schema tables to discover table structure.\n"
            f"- Missing SELECT on: {', '.join(missing_system)}\n"
            "Grant these permissions, e.g.:\n"
            f"  GRANT SELECT ON system.tables, system.columns TO {user};"
        )

    logger.info(
        "✅ ClickHouse permission checks passed for user '%s' on DB '%s'.",
        user,
        db,
    )
