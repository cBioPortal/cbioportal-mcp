"""Check that a database's row policies cover every table and view (admin-run).

For the policy target (the restricted role, or a user), against ``catalog.py``:

- the database-wide default-deny (``ON db.* USING 0``) exists and no other
  database-wide policy opens every table;
- every study-scoped table has its own policy that reads ``user_study`` with
  the caller-email predicate (never ``USING 1``);
- ``user_study`` has exactly the identity predicate, and is a refreshable
  materialized view without ``TO`` or ``APPEND`` (``user_study_shape_error``);
  it is the ONLY materialized view accepted, and its last refresh succeeded;
- every reference table and view has a policy, and every view uses
  ``SQL SECURITY INVOKER`` (explicitly or implicitly) (DEFINER / NONE views leak every study);
- denied tables, including ``user_study``'s implicit inner table and its
  refresh staging copy, have no policy; no other materialized view and no
  dictionaries (dictGet bypasses row policies);
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
    USER_STUDY_TABLE,
    user_study_inner_tables,
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


def _unquoted_sql(sql: str) -> str | None:
    """Mask SQL literals, quoted identifiers and comments; reject unterminated tokens."""
    out, i = [], 0
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(sql):
                if sql[i] == "\\":
                    i += 2
                elif sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                return None
            out.append(" ")
        elif sql.startswith("--", i) or ch == "#":
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end + 1
            out.append(" ")
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                return None
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def view_sql_security(create_query: str) -> str | None:
    """Security of a stored CREATE VIEW, shared by startup and the admin check.

    Neither 25.8 nor 26.6 exposes a structured security field in system.tables.
    Both execute a stored clause-less normal view as INVOKER, even with
    default_normal_view_sql_security=DEFINER (live regression tested).
    """
    sql = _unquoted_sql(create_query)
    if sql is None:
        return None
    clauses = list(re.finditer(r"\bSQL\s+SECURITY\b", sql, re.I))
    if len(clauses) > 1:
        return None
    if clauses:
        mode = re.match(r"\s+(INVOKER|DEFINER|NONE)\b", sql[clauses[0].end() :], re.I)
        return mode.group(1).upper() if mode else None
    if re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b", sql, re.I) and re.search(
        r"\bAS\s+\(*\s*(SELECT|WITH)\b", sql, re.I
    ):
        return "INVOKER"
    return None


def user_study_shape_error(engine: str, create_query: str) -> str | None:
    """Why ``user_study`` is not the expected refreshable view, or None if it is.

    Expected: ``CREATE MATERIALIZED VIEW ... REFRESH EVERY|AFTER ...`` with an
    implicit inner table. Refused: a plain table (the previous layout, written
    by a separate ``... TO user_study`` refresh whose swap renames the table a
    running query holds), a ``TO`` view (same swap), ``APPEND`` (rows are never
    replaced, so revoked grants would stay), or a non-refreshable view.
    """
    if engine != "MaterializedView":
        return (
            f"{engine} table, not a refreshable materialized view (previous layout; "
            "migrate with `apply --migrate`, docs/study-authz.md)"
        )
    sql = _unquoted_sql(create_query)
    if sql is None or not re.match(r"\s*CREATE\s+MATERIALIZED\s+VIEW\b", sql, re.I):
        return "materialized view definition is unreadable"
    # Everything before the column list / AS: names are masked by _unquoted_sql.
    head = re.split(r"\(|\bAS\b", sql, maxsplit=1, flags=re.I)[0]
    if not re.search(r"\bREFRESH\s+(?:EVERY|AFTER)\b", head, re.I):
        return "materialized view is not refreshable"
    if re.search(r"\bAPPEND\b", head, re.I):
        return "refreshable view uses APPEND (stale grants would never be removed)"
    if re.search(r"\bTO\b", head, re.I):
        return "refreshable view writes TO another table (its refresh swap races policy lookup)"
    return None


def _foreign_databases(select_filter: str, database: str) -> set[str]:
    refs = set(re.findall(r"\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_]+)`?\s*\.", select_filter, re.I))
    return {ref for ref in refs if ref != database}


def evaluate(
    objects: dict[str, tuple[str, str]],
    policies: list[Policy],
    database: str,
    refresh_error: str | None = None,
    conflicts: list[str] = (),
    user_study_uuid: str = "",
) -> Report:
    """Pure check over fetched metadata: ``objects`` is name -> (engine, create query).

    ``conflicts`` is ``sqlgen.policy_conflicts`` for the database and target.
    ``user_study_uuid`` (``fetch_user_study_uuid``) names its inner tables.
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

    inner_tables = user_study_inner_tables(user_study_uuid)
    for name in sorted(objects):
        engine, create_query = objects[name]
        own = by_table.get(name, [])
        filters = [_squash(p.select_filter) for p in own]

        if name in inner_tables:
            if own:
                report.errors.append(
                    f"{name}: user_study's inner table has policies {[p.name for p in own]}"
                )
            if engine != "MergeTree":
                report.errors.append(f"{name}: user_study's inner table is {engine}, not MergeTree")
            continue
        if name in DENIED_TABLES:
            if own:
                report.errors.append(f"{name}: denied object has policies {[p.name for p in own]}")
            continue
        if name in IDENTITY_SCOPED_TABLES:
            shape = user_study_shape_error(engine, create_query)
            if shape:
                report.errors.append(f"{name}: {shape}")
            if not filters or any(f != _MATCH for f in filters):
                report.errors.append(f"{name}: policy must be exactly {USER_STUDY_MATCH}")
            if refresh_error:
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


def fetch_user_study_uuid(client, database: str) -> str:
    """``user_study``'s uuid ('' if absent), which names its inner tables."""
    rows = client.query(
        "SELECT toString(uuid) FROM system.tables "
        "WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": USER_STUDY_TABLE},
    ).result_rows
    return str(rows[0][0]) if rows else ""


def fetch_refresh_error(client, database: str) -> str | None:
    refresh = client.query(
        "SELECT exception, last_success_time FROM system.view_refreshes "
        "WHERE database = {db:String} AND view = {v:String}",
        parameters={"db": database, "v": USER_STUDY_TABLE},
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
    if objects.get(USER_STUDY_TABLE, ("",))[0] == "MaterializedView":
        refresh_error = fetch_refresh_error(client, database)
    return evaluate(
        objects,
        policies,
        database,
        refresh_error,
        policy_conflicts(client, database, target),
        user_study_uuid=fetch_user_study_uuid(client, database),
    )
