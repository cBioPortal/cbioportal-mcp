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

If checks fail, we raise PermissionError so the application can fail fast.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List

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
        from cbioportal_mcp.study_authz.startup_check import redact

        logger.warning(
            "CHECK GRANT %s ON %s failed (treating as not granted): %s",
            priv,
            scope,
            redact(e),
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
        * Schema discovery (SHOW TABLES, DESCRIBE TABLE) is implicitly
          allowed when SELECT is granted on the database.

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
        # One line: startup logs it as a single CRITICAL record.
        raise PermissionError(
            f"the MCP ClickHouse user '{user}' lacks SELECT ON {db}.*; "
            f"grant minimally: GRANT SELECT ON {db}.* TO {user}"
        )

    bad_privs = _forbidden_privs_present()
    if bad_privs:
        privs = ", ".join(sorted(bad_privs))
        raise PermissionError(
            f"the MCP ClickHouse user '{user}' has forbidden privileges on *.* ({privs}) "
            f"and must be strictly read-only: REVOKE {privs} ON *.* FROM {user}"
        )

    logger.info(
        "✅ ClickHouse permission checks passed for user '%s' on DB '%s'.",
        user,
        db,
    )
