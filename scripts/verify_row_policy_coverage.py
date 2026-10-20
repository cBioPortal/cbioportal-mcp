#!/usr/bin/env python3
"""Verify the MCP's restricted role has correct row-policy coverage on every
table, view and dictionary in a ClickHouse database.

Checks, for the restricted role (default ``cbioportal_mcp_study_restricted``),
against the classification in
``src/cbioportal_mcp/authentication/row_policy_catalog.py``:

- the database-wide default-deny (``ON db.* USING 0``) exists, and no other
  database-wide policy opens every table;
- every study-scoped table has its own policy, and that policy actually
  filters on the allowed-studies setting (not ``USING 1``) - a study-scoped
  table caught only by the default-deny is flagged as *fully denied by
  accident* (it returns nothing instead of the caller's rows);
- every reference table and every view has a policy (``USING 1`` is expected:
  the default-deny matches views too, and a view's rows are filtered by its
  underlying tables' policies);
- no view uses ``SQL SECURITY DEFINER`` (it would read as its definer and
  bypass the underlying tables' policies);
- no materialized view or dictionary exists in the database: dictGet() reads
  a dictionary's source with the dictionary's own credentials, bypassing row
  policies, and a materialized view's target table needs its own decision;
- denied tables (cBioPortal's auth tables) have no policy;
- every policy subquery references only the target database (an
  unsubstituted ``__CLICKHOUSE_DATABASE__`` placeholder or a different
  database is an error);
- every other (unclassified) table or view is reported: the default-deny
  keeps it safe, but it is unreadable until someone classifies it.

This checks presence and shape, not row-level correctness; the local e2e
(``docker/local-e2e``) runs unfiltered SELECTs as restricted users for that.

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
migrations) to catch drift.

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
  CLICKHOUSE_ADMIN_USER   NOT CLICKHOUSE_USER - needs SHOW TABLES /
                          SHOW DICTIONARIES on the database and SELECT on
                          system.row_policies and system.dictionaries (no
                          row data access needed)
  CLICKHOUSE_ADMIN_PASSWORD

Optional env:
  CLICKHOUSE_PORT                        default 8443 (HTTPS)
  CLICKHOUSE_SECURE                      default true
  CBIOPORTAL_MCP_CLICKHOUSE_ROLE         default cbioportal_mcp_study_restricted
  CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING
                                         default SQL_cbiomcp_allowed_studies
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

import clickhouse_connect

from cbioportal_mcp.authentication.row_policy_catalog import (
    DENIED_TABLES,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
)

VIEW_ENGINES = {"View"}
BYPASS_ENGINES = {"MaterializedView", "Dictionary", "LiveView", "WindowView"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


@dataclass(frozen=True)
class Policy:
    name: str
    table: str  # '' for a database-wide (db.*) policy
    select_filter: str
    is_restrictive: bool = False


@dataclass
class CoverageReport:
    errors: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unclassified


class NoTablesVisibleError(RuntimeError):
    """system.tables returned nothing for the target database.

    Almost certainly means the admin identity itself lacks visibility (no
    SHOW TABLES / SELECT grant on the target database) rather than the
    database genuinely being empty - system.tables is scoped to what the
    querying user can see, not a full catalog. Silently treating "0 tables"
    as "0 tables missing coverage" would make a misconfigured admin user
    look like a clean pass, which defeats the point of this check.
    """


def _objects_in_database(client, database: str) -> dict[str, tuple[str, str]]:
    """name -> (engine, create_table_query) for every table/view/dictionary."""
    result = client.query(
        "SELECT name, engine, create_table_query FROM system.tables "
        "WHERE database = {db:String} AND NOT is_temporary",
        parameters={"db": database},
    )
    objects = {row[0]: (row[1], row[2] or "") for row in result.result_rows}
    # Dictionaries loaded from config files are not in system.tables.
    try:
        dictionaries = client.query(
            "SELECT name FROM system.dictionaries WHERE database = {db:String}",
            parameters={"db": database},
        )
    except Exception as e:
        raise SystemExit(
            "error: could not read system.dictionaries - grant the admin user "
            f"SELECT ON system.dictionaries ({e})"
        ) from e
    for row in dictionaries.result_rows:
        objects.setdefault(row[0], ("Dictionary", ""))
    return objects


def _policies_for_role(client, database: str, role: str) -> list[Policy]:
    result = client.query(
        """
        SELECT short_name, table, select_filter, is_restrictive
        FROM system.row_policies
        WHERE database = {db:String}
          AND (apply_to_all OR has(apply_to_list, {role:String}))
          AND NOT has(apply_to_except, {role:String})
        """,
        parameters={"db": database, "role": role},
    )
    return [
        Policy(name=row[0], table=row[1], select_filter=row[2] or "", is_restrictive=bool(row[3]))
        for row in result.result_rows
    ]


def _is_constant(select_filter: str, value: str) -> bool:
    return select_filter.strip().strip("()").strip() == value


def _foreign_database_refs(select_filter: str, database: str) -> set[str]:
    """Databases other than ``database`` that a policy filter's FROM/JOIN names."""
    refs = set(re.findall(r"\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_]+)`?\s*\.", select_filter, flags=re.I))
    if "__CLICKHOUSE_DATABASE__" in select_filter:
        refs.add("__CLICKHOUSE_DATABASE__")
    return {ref for ref in refs if ref != database}


def evaluate_coverage(
    objects: dict[str, tuple[str, str]],
    policies: list[Policy],
    database: str,
    setting_name: str,
) -> CoverageReport:
    """Pure check over already-fetched metadata (see module docstring)."""
    report = CoverageReport()
    by_table: dict[str, list[Policy]] = {}
    for policy in policies:
        if not policy.is_restrictive:
            by_table.setdefault(policy.table, []).append(policy)

    wildcard = by_table.get("", [])
    if not any(_is_constant(p.select_filter, "0") for p in wildcard):
        report.errors.append(
            f"no database-wide default-deny policy (ON {database}.* USING 0) for the role: "
            "unclassified tables would be fully readable"
        )
    for policy in wildcard:
        if not _is_constant(policy.select_filter, "0"):
            report.errors.append(
                f"database-wide policy {policy.name} ({policy.select_filter}) is not USING 0: "
                "it applies to every table, including unclassified ones"
            )

    for policy in policies:
        foreign = _foreign_database_refs(policy.select_filter, database)
        if foreign:
            report.errors.append(
                f"{policy.table or '*'}: policy {policy.name} reads other database(s) "
                f"{sorted(foreign)}; every subquery must be qualified with {database}"
            )

    for name in sorted(objects):
        engine, create_query = objects[name]
        own = by_table.get(name, [])

        if engine in BYPASS_ENGINES:
            report.errors.append(
                f"{name}: {engine} - row policies do not cover it the way they cover plain "
                "tables (dictGet / materialized targets read with their own rights); remove "
                "it from the MCP database or handle it explicitly"
            )
            continue

        if engine in VIEW_ENGINES:
            if re.search(r"\bSQL\s+SECURITY\s+DEFINER\b", create_query, flags=re.I):
                report.errors.append(
                    f"{name}: view uses SQL SECURITY DEFINER, so it reads as its definer and "
                    "bypasses the underlying tables' row policies; recreate it as INVOKER"
                )
            if not own:
                if name in STUDY_SCOPED_VIEWS:
                    report.errors.append(
                        f"{name}: view has no pass-through policy - fully denied by the "
                        "default-deny (returns no rows for restricted users)"
                    )
                else:
                    report.unclassified.append(name)
            continue

        if name in DENIED_TABLES:
            if own:
                report.errors.append(
                    f"{name}: deliberately denied table has row policies "
                    f"{[p.name for p in own]}; remove them"
                )
            continue

        if name in STUDY_SCOPED_TABLES:
            if not own:
                report.errors.append(
                    f"{name}: study-scoped table has only the default-deny - fully denied by "
                    f"accident (expected a policy via {STUDY_SCOPED_TABLES[name]})"
                )
            elif any(_is_constant(p.select_filter, "1") for p in own):
                report.errors.append(
                    f"{name}: study-scoped table has a USING 1 policy (unfiltered)"
                )
            elif not all(setting_name in p.select_filter for p in own):
                report.errors.append(
                    f"{name}: study-scoped policy does not filter on {setting_name}"
                )
            continue

        if name in STUDY_AGNOSTIC_REFERENCE_TABLES:
            if not own:
                report.errors.append(
                    f"{name}: reference table has only the default-deny - fully denied by "
                    "accident (expected USING 1)"
                )
            continue

        report.unclassified.append(name)

    return report


def check_coverage(client, database: str, role: str, setting_name: str) -> CoverageReport:
    objects = _objects_in_database(client, database)
    if not objects:
        raise NoTablesVisibleError(
            f"system.tables returned no tables for database '{database}'. "
            "Either the database is genuinely empty, or (far more likely) "
            "the admin user lacks SHOW TABLES / SELECT visibility on it - "
            "system.tables only shows what the querying user can see."
        )
    policies = _policies_for_role(client, database, role)
    return evaluate_coverage(objects, policies, database, setting_name)


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
        "--setting",
        default=os.getenv(
            "CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING", "SQL_cbiomcp_allowed_studies"
        ),
        help="Custom setting carrying the allowed studies (default: %(default)s)",
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
        report = check_coverage(client, args.database, args.role, args.setting)
    except NoTablesVisibleError as e:
        raise SystemExit(f"error: {e}") from e

    if report.errors:
        print(f"\nRow-policy problems ({len(report.errors)}):")
        for error in report.errors:
            print(f"  - {error}")
    if report.unclassified:
        print(
            f"\nUnclassified tables/views ({len(report.unclassified)}) - kept unreadable by the "
            "default-deny, but nobody has decided how they map to a study:"
        )
        for name in report.unclassified:
            print(f"  - {name}")
    if not report.ok:
        print(
            "\nClassify each in src/cbioportal_mcp/authentication/row_policy_catalog.py and add "
            "the matching policy to sql/study-authz/0-row-policies.sql "
            "(tests/test_row_policy_sql.py keeps the two in sync)."
        )
        sys.exit(1)

    print(
        "OK: default-deny present; every study-scoped table filters on the allowed-studies "
        "setting; reference tables and views are explicitly readable; no DEFINER views, "
        "materialized views or dictionaries."
    )


if __name__ == "__main__":
    main()
