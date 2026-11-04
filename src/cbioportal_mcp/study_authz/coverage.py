"""Check that a database's row policies cover every table and view (admin-run).

For the policy target (the restricted role, or a user), against ``catalog.py``:

- the database-wide default-deny (``ON db.* USING 0``) exists and no other
  database-wide policy opens every table;
- every study-scoped table has its own policy that reads ``user_study`` with
  the caller-email predicate (never ``USING 1``);
- ``user_study`` has exactly the identity predicate;
- every reference table and view has a policy, and every view is explicitly
  ``SQL SECURITY INVOKER`` (DEFINER / NONE views leak every study);
- denied tables have no policy; the only materialized view is
  ``user_study_refresh``, and its last refresh succeeded; no dictionaries
  (dictGet bypasses row policies);
- policy subqueries reference only the target database;
- no row policy on the database was created by anyone else, or attaches our
  policies to another target (``sqlgen.policy_conflicts``): the catch-all
  ``USING 1 TO ALL EXCEPT <target>`` would widen it;
- anything unclassified fails the check: the default-deny keeps it safe, but
  nobody has decided how it maps to a study.

Presence and shape only; the live tests prove row-level behavior. The admin
CLI reads with admin credentials; the server's own startup check
(``startup_check.py``, filtering on) reuses ``evaluate`` as the runtime user,
which needs ``SHOW ROW POLICIES ON <db>.*`` (granted by the policy template).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cbioportal_mcp.study_authz.catalog import (
    DENIED_TABLES,
    IDENTITY_SCOPED_TABLES,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLE_PATHS,
    STUDY_SCOPED_VIEWS,
    USER_STUDY_REFRESH_VIEW,
    USER_STUDY_TABLE,
)
from cbioportal_mcp.study_authz.sqlgen import (
    USER_STUDY_MATCH,
    policy_conflicts,
    validate_identifier,
)

BYPASS_ENGINES = {"MaterializedView", "Dictionary", "LiveView", "WindowView"}


@dataclass(frozen=True)
class Policy:
    name: str
    table: str  # '' for a database-wide (db.*) policy
    select_filter: str
    is_restrictive: bool = False


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unclassified


def _squash(text: str) -> str:
    """Filter text without parentheses/whitespace/backslashes (ClickHouse reformats)."""
    return re.sub(r"[()\s\\]", "", text)


_MATCH = _squash(USER_STUDY_MATCH)


def view_sql_security(create_query: str) -> str | None:
    match = re.search(r"\bSQL\s+SECURITY\s+(INVOKER|DEFINER|NONE)\b", create_query, re.I)
    return match.group(1).upper() if match else None


def _foreign_databases(select_filter: str, database: str) -> set[str]:
    refs = set(re.findall(r"\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_]+)`?\s*\.", select_filter, re.I))
    return {ref for ref in refs if ref != database}


def evaluate(
    objects: dict[str, tuple[str, str]],
    policies: list[Policy],
    database: str,
    refresh_error: str | None = None,
    conflicts: list[str] = (),
) -> Report:
    """Pure check over fetched metadata: ``objects`` is name -> (engine, create query).

    ``conflicts`` is ``sqlgen.policy_conflicts`` for the database and target.
    """
    report = Report()
    for conflict in conflicts:
        report.errors.append(f"row policy the catch-all would widen: {conflict}")
    by_table: dict[str, list[Policy]] = {}
    for policy in policies:
        if policy.is_restrictive:
            report.errors.append(f"{policy.table or '*'}: unexpected RESTRICTIVE {policy.name}")
        by_table.setdefault(policy.table, []).append(policy)

    wildcard = by_table.get("", [])
    if not any(_squash(p.select_filter) == "0" for p in wildcard):
        report.errors.append(f"no database-wide default-deny (ON {database}.* USING 0)")
    for policy in wildcard:
        if _squash(policy.select_filter) != "0":
            report.errors.append(f"database-wide policy {policy.name} is not USING 0")
    for policy in policies:
        foreign = _foreign_databases(policy.select_filter, database)
        if foreign:
            report.errors.append(f"{policy.table}: {policy.name} reads {sorted(foreign)}")

    for name in sorted(objects):
        engine, create_query = objects[name]
        own = by_table.get(name, [])
        filters = [_squash(p.select_filter) for p in own]

        if name in DENIED_TABLES:
            if own:
                report.errors.append(f"{name}: denied object has policies {[p.name for p in own]}")
            if name == USER_STUDY_REFRESH_VIEW and refresh_error:
                report.errors.append(f"{name}: {refresh_error}")
            continue
        if engine in BYPASS_ENGINES:
            report.errors.append(f"{name}: {engine} bypasses row policies; remove it")
            continue
        if engine == "View":
            security = view_sql_security(create_query)
            if security != "INVOKER":
                report.errors.append(f"{name}: view is SQL SECURITY {security or 'default'}")
            if name not in STUDY_SCOPED_VIEWS:
                report.unclassified.append(name)
            elif not own:
                report.errors.append(f"{name}: view has no pass-through policy (fully denied)")
            continue
        if name in IDENTITY_SCOPED_TABLES:
            if not filters or any(f != _MATCH for f in filters):
                report.errors.append(f"{name}: policy must be exactly {USER_STUDY_MATCH}")
            continue
        if name in STUDY_SCOPED_TABLE_PATHS:
            if not own:
                report.errors.append(f"{name}: study table has only the default-deny")
            elif any(f == "1" for f in filters):
                report.errors.append(f"{name}: study table has a USING 1 policy")
            elif not all(f"{database}.{USER_STUDY_TABLE}" in f and _MATCH in f for f in filters):
                report.errors.append(f"{name}: policy does not filter through user_study")
            continue
        if name in STUDY_AGNOSTIC_REFERENCE_TABLES:
            if not own:
                report.errors.append(f"{name}: reference table has only the default-deny")
            continue
        report.unclassified.append(name)

    if USER_STUDY_TABLE not in objects:
        report.errors.append(f"{USER_STUDY_TABLE} is missing (apply 0-user-study first)")
    if USER_STUDY_REFRESH_VIEW not in objects:
        report.errors.append(f"{USER_STUDY_REFRESH_VIEW} is missing: nothing refreshes grants")
    return report


@dataclass(frozen=True)
class PolicyRow:
    """A ``system.row_policies`` row: the policy plus who it applies TO."""

    policy: Policy
    apply_to_all: bool = False
    apply_to_list: tuple[str, ...] = ()
    apply_to_except: tuple[str, ...] = ()

    def applies_to(self, principals: set[str]) -> bool:
        """ClickHouse's match: the user or any of its *enabled* roles."""
        listed = self.apply_to_all or bool(principals & set(self.apply_to_list))
        return listed and not principals & set(self.apply_to_except)


def fetch_objects(
    client, database: str, *, dictionaries: bool = True
) -> dict[str, tuple[str, str]]:
    """name -> (engine, create query) for every table, view and dictionary.

    ``dictionaries=False`` skips ``system.dictionaries``, which a non-admin
    user cannot read; DDL dictionaries also appear in ``system.tables``.
    """
    tables = client.query(
        "SELECT name, engine, create_table_query FROM system.tables "
        "WHERE database = {db:String} AND NOT is_temporary",
        parameters={"db": database},
    ).result_rows
    objects = {row[0]: (row[1], row[2] or "") for row in tables}
    if dictionaries:
        for (name,) in client.query(
            "SELECT name FROM system.dictionaries WHERE database = {db:String}",
            parameters={"db": database},
        ).result_rows:
            objects.setdefault(name, ("Dictionary", ""))
    return objects


def fetch_policy_rows(client, database: str) -> list[PolicyRow]:
    """Every row policy on ``database``, whoever it applies to."""
    rows = client.query(
        "SELECT short_name, table, select_filter, is_restrictive, apply_to_all, "
        "apply_to_list, apply_to_except FROM system.row_policies WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    return [
        PolicyRow(
            Policy(r[0], r[1], r[2] or "", bool(r[3])),
            bool(r[4]),
            tuple(r[5] or ()),
            tuple(r[6] or ()),
        )
        for r in rows
    ]


def fetch_refresh_error(client, database: str) -> str | None:
    refresh = client.query(
        "SELECT exception, last_success_time FROM system.view_refreshes "
        "WHERE database = {db:String} AND view = {v:String}",
        parameters={"db": database, "v": USER_STUDY_REFRESH_VIEW},
    ).result_rows
    if not refresh:
        return "not scheduled (no row in system.view_refreshes)"
    if refresh[0][0]:
        return f"last refresh failed: {str(refresh[0][0]).splitlines()[0][:200]}"
    if refresh[0][1] is None:
        return "has never refreshed successfully"
    return None


def fetch_and_evaluate(client, database: str, target: str) -> Report:
    """``target``: the role or user the policies are attached TO."""
    validate_identifier("database", database)
    validate_identifier("policy target", target)
    objects = fetch_objects(client, database)
    if not objects:
        raise SystemExit(f"no tables visible in {database}: wrong database or admin lacks SHOW")
    policies = [r.policy for r in fetch_policy_rows(client, database) if r.applies_to({target})]
    refresh_error = None
    if USER_STUDY_REFRESH_VIEW in objects:
        refresh_error = fetch_refresh_error(client, database)
    return evaluate(
        objects, policies, database, refresh_error, policy_conflicts(client, database, target)
    )
