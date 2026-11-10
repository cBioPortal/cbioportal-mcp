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

import logging
from typing import List

from cbioportal_mcp.env import McpConfig
from cbioportal_mcp.redaction import describe_error

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


def _open_client():
    """A fresh client built exactly like mcp-clickhouse's own.

    The permission gate does not go through upstream ``run_query``: its
    worker logs raw exception text (which can carry credentials) before any
    caller could sanitize it. Errors here are reported as class + code only.
    """
    from mcp_clickhouse.mcp_server import create_clickhouse_client

    try:
        return create_clickhouse_client()
    except Exception as e:  # noqa: BLE001 - never echo driver text
        raise PermissionError(
            f"cannot connect to ClickHouse to run CHECK GRANT ({describe_error(e)}); "
            "check the CLICKHOUSE_* settings"
        ) from None


def _check_grant(client, priv: str, scope: str) -> bool:
    """
    Use CHECK GRANT <priv> ON <scope> to see if the current user has a privilege.

    Valid scopes include:
      - "<db>.*"
      - "*.*"
      - "<db>.table[*]" (not used here, but legal)

    Returns True iff result == 1. A failed query counts as not granted and is
    logged as exception class + ClickHouse code only.

    Important: CHECK GRANT may return a row with no column names, so we read
    the first value from rows[0][0] rather than relying on column metadata.
    """
    scope = scope.strip()
    if scope == "*":
        scope = "*.*"

    try:
        rows = client.query(f"CHECK GRANT {priv} ON {scope}").result_rows
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "CHECK GRANT %s ON %s failed (treating as not granted): %s",
            priv,
            scope,
            describe_error(e),
        )
        return False
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


def _forbidden_privs_present(client) -> List[str]:
    """
    Returns a list of forbidden privileges for which CHECK GRANT ... ON *.* is true.
    """
    bad: List[str] = []
    for p in FORBIDDEN_PRIVS:
        if _check_grant(client, p, "*.*"):
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

    client = _open_client()
    try:
        has_select = _check_grant(client, "SELECT", f"{db}.*")
        bad_privs = _forbidden_privs_present(client) if has_select else []
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    if not has_select:
        # One line: startup logs it as a single CRITICAL record.
        raise PermissionError(
            f"the MCP ClickHouse user '{user}' lacks SELECT ON {db}.*; "
            f"grant minimally: GRANT SELECT ON {db}.* TO {user}"
        )

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
