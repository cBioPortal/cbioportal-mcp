#!/usr/bin/env python3
"""Verify every table in a ClickHouse database has row-policy coverage for
the MCP's restricted role, or is explicitly allowlisted as study-agnostic
reference data.

Why this is a separate, admin-run script and not a runtime startup check in
the MCP server process: reading row-policy definitions requires
`SELECT ON system.row_policies`, which is not scoped to the querying user's
own access - once granted, it returns policy metadata (database names,
table names, role names) for every database on the cluster, not just the
MCP's configured one. Granting that to the MCP's low-privilege runtime role
would expose it through the `clickhouse_run_select_query` arbitrary-SQL
tool. Using admin credentials here (never loaded by the running server -
same split `apply_sql.sh` uses) avoids widening the runtime role at all.

Run this before enabling CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
in a deployment, and periodically after (e.g. after cBioPortal schema
migrations) to catch drift - a new table added with no row policy of its
own.

This is the row-level counterpart to the server's own startup check
(cbioportal_mcp.authentication.permissions.ensure_db_permissions), which
only ever verifies database-wide GRANT breadth - whether a table is
reachable at all - and deliberately does not check ROW POLICY coverage -
which rows come back once it is. See that module's docstring for the full
reasoning on why the two checks live in different places with different
credentials, rather than one being folded into the other.

Required env (or flags):
  CLICKHOUSE_HOST
  CLICKHOUSE_DATABASE     target database (e.g. the prepped LLM clone)
  CLICKHOUSE_ADMIN_USER   NOT CLICKHOUSE_USER - must be able to read
                          system.row_policies (the same user used by
                          apply_sql.sh, or another with equivalent rights)
  CLICKHOUSE_ADMIN_PASSWORD

Optional env:
  CLICKHOUSE_PORT                        default 8443 (HTTPS)
  CLICKHOUSE_SECURE                      default true
  CBIOPORTAL_MCP_CLICKHOUSE_ROLE         default cbioportal_mcp_study_restricted
"""

from __future__ import annotations

import argparse
import os
import sys

import clickhouse_connect

from cbioportal_mcp.authentication.study_access import STUDY_AGNOSTIC_REFERENCE_TABLES


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"error: {name} is required")
    return value


def _tables_in_database(client, database: str) -> set[str]:
    result = client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} AND engine != 'View'",
        parameters={"db": database},
    )
    return {row[0] for row in result.result_rows}


def _tables_with_row_policy(client, database: str, role: str) -> set[str]:
    # `table != ''` excludes database-wide wildcard policies (e.g. the
    # default-deny fallback on `db.*`) - those keep an unscoped table safe,
    # but this check wants tables that have been deliberately scoped, not
    # ones merely caught by the fail-closed default.
    result = client.query(
        """
        SELECT DISTINCT table
        FROM system.row_policies
        WHERE database = {db:String}
          AND table != ''
          AND (apply_to_all OR has(apply_to_list, {role:String}))
          AND NOT has(apply_to_except, {role:String})
        """,
        parameters={"db": database, "role": role},
    )
    return {row[0] for row in result.result_rows}


class NoTablesVisibleError(RuntimeError):
    """system.tables returned nothing for the target database.

    Almost certainly means the admin identity itself lacks visibility (no
    SHOW TABLES / SELECT grant on the target database) rather than the
    database genuinely being empty - system.tables is scoped to what the
    querying user can see, not a full catalog. Silently treating "0 tables"
    as "0 tables missing coverage" would make a misconfigured admin user
    look like a clean pass, which defeats the point of this check.
    """


def check_coverage(client, database: str, role: str) -> list[str]:
    """Return the sorted list of tables missing row-policy coverage."""
    tables = _tables_in_database(client, database)
    if not tables:
        raise NoTablesVisibleError(
            f"system.tables returned no tables for database '{database}'. "
            "Either the database is genuinely empty, or (far more likely) "
            "the admin user lacks SHOW TABLES / SELECT visibility on it - "
            "system.tables only shows what the querying user can see."
        )
    covered = _tables_with_row_policy(client, database, role)
    return sorted(tables - covered - STUDY_AGNOSTIC_REFERENCE_TABLES)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=os.getenv("CLICKHOUSE_HOST"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLICKHOUSE_PORT", "8443")))
    parser.add_argument("--database", default=os.getenv("CLICKHOUSE_DATABASE"))
    parser.add_argument("--user", default=os.getenv("CLICKHOUSE_ADMIN_USER"))
    parser.add_argument("--password", default=os.getenv("CLICKHOUSE_ADMIN_PASSWORD"))
    parser.add_argument(
        "--role",
        default=os.getenv("CBIOPORTAL_MCP_CLICKHOUSE_ROLE", "cbioportal_mcp_study_restricted"),
        help="ClickHouse role the MCP's restricted runtime user is granted (default: %(default)s)",
    )
    parser.add_argument(
        "--secure", action="store_true", default=_env_bool("CLICKHOUSE_SECURE", True)
    )
    parser.add_argument("--no-secure", dest="secure", action="store_false")
    args = parser.parse_args()

    if not args.host:
        raise SystemExit("error: --host or CLICKHOUSE_HOST is required")
    if not args.database:
        raise SystemExit("error: --database or CLICKHOUSE_DATABASE is required")
    if not args.user:
        raise SystemExit(
            "error: --user or CLICKHOUSE_ADMIN_USER is required - "
            "do not use the MCP SELECT-only user"
        )
    if args.password is None:
        raise SystemExit(
            "error: --password or CLICKHOUSE_ADMIN_PASSWORD is required (may be empty)"
        )

    client = clickhouse_connect.get_client(
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        secure=args.secure,
    )

    print(f"Target: {args.user}@{args.host}:{args.port}/{args.database} (role: {args.role})")
    try:
        gap = check_coverage(client, args.database, args.role)
    except NoTablesVisibleError as e:
        raise SystemExit(f"error: {e}") from e

    if gap:
        print(f"\nRow-policy coverage gap - {len(gap)} table(s) with no policy for '{args.role}':")
        for table in gap:
            print(f"  - {table}")
        print(
            "\nEach table above needs its own CREATE ROW POLICY for "
            f"{args.role} (a direct cancer_study_identifier check, or a "
            "subquery joining back to a table that has one), or should be "
            "added to STUDY_AGNOSTIC_REFERENCE_TABLES in "
            "src/cbioportal_mcp/authentication/study_access.py if it is "
            "genuinely study-agnostic reference data."
        )
        sys.exit(1)

    print("OK: every table has row-policy coverage (or is an allowlisted reference table).")


if __name__ == "__main__":
    main()
