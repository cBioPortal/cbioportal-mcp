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

Scope, deliberately: this module only ever checks database-wide GRANT
breadth via `CHECK GRANT ... ON db.*` / `*.*`. It never enumerates
individual tables, and it never checks ROW POLICY coverage (whether a
specific table's rows are actually scoped to a study). Those are a
different privilege axis in ClickHouse - GRANT controls whether a table is
reachable at all; ROW POLICY controls which rows come back once it is - and
checking one tells you nothing about the other.

Row-policy coverage is deliberately NOT added here, even though it sounds
like the same kind of "is this deployment configured correctly" question.
Verifying it requires reading `system.row_policies`, which is not scoped to
the querying user's own database access - once granted, it exposes policy
metadata for every database on the cluster, not just this one. Granting
that to this module's low-privilege runtime user (config.mcp_user, the same
identity behind the `clickhouse_run_select_query` arbitrary-SQL tool) would
leak that metadata to anyone using the tool. So row-policy coverage lives in
a standalone, admin-credentialed script instead -
scripts/verify_row_policy_coverage.py, run at deploy time (before enabling
CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true) and periodically after,
never loaded by this running process.

Also deliberately a startup check, not a per-call one: the property here
(this user's static grants) doesn't change between requests within a
running process, and ClickHouse itself already enforces the real access
control on every query regardless of what this module does - if a grant
were revoked mid-session, the next query would fail with ClickHouse's own
ACCESS_DENIED. This check exists purely to fail fast and loud at startup,
before any traffic is served, rather than have a misconfiguration surface
as a confusing error on whichever request happens to hit it first.
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

    logger.info(
        "✅ ClickHouse permission checks passed for user '%s' on DB '%s'.",
        user,
        db,
    )
