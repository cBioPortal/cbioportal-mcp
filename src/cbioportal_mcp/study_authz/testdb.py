"""Build a small study-authz test database from a few studies of a source database.

Copies only the chosen studies' rows of every study-scoped table (using the
same catalog FK paths as the policies), copies reference tables whole,
recreates the catalog views, rewrites ``cancer_study.groups`` so gated
studies carry a private group and the rest the public group, applies the
study-authz SQL, and seeds ``user_study_manual``. Auth tables (users,
authorities, ...) are never copied. See docs/study-authz.md for usage.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cbioportal_mcp.study_authz.catalog import (
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLE_PATHS,
    STUDY_SCOPED_VIEWS,
    USER_STUDY_MANUAL_TABLE,
    USER_STUDY_REFRESH_VIEW,
)
from cbioportal_mcp.study_authz.groups import NAME_RE, validate_name
from cbioportal_mcp.study_authz.sqlgen import (
    Params,
    apply,
    quote_identifier,
    render_path,
    sql_string,
    validate_identifier,
)

MARKER = "study-authz test database (make-test-db)"
DEFAULT_GRANT_DAYS = 7


@dataclass(frozen=True)
class Grant:
    email: str
    grant_key: str
    valid_until: datetime


def read_grants_csv(path: Path, now: datetime | None = None) -> list[Grant]:
    """CSV with a header: email,grant_key[,valid_until] (ISO 8601, UTC if no offset).

    A missing/empty valid_until means now + DEFAULT_GRANT_DAYS days.
    """
    now = now or datetime.now(timezone.utc)
    grants = []
    with path.open(newline="", encoding="utf-8") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            email = (row.get("email") or "").strip().lower()
            key = (row.get("grant_key") or "").strip()
            if "@" not in email or not NAME_RE.match(key):
                raise ValueError(f"{path}:{line}: need an email and a grant_key ({row})")
            raw = (row.get("valid_until") or "").strip()
            if raw:
                until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            else:
                until = now + timedelta(days=DEFAULT_GRANT_DAYS)
            grants.append(Grant(email, key, until))
    return grants


def _in_list(ids: list[str]):
    values = ", ".join(sql_string(i) for i in ids)
    return lambda column, depth: f"{column} IN ({values})"


def _rewrite_view(create_query: str, source: str, target: str) -> str:
    """Point a view's database references at the target (both names validated)."""
    validate_identifier("source database", source)
    validate_identifier("database", target)
    return re.sub(rf"`?\b{re.escape(source)}\b`?\.", f"`{target}`.", create_query)


def make_test_db(
    client,
    source: str,
    params: Params,
    gated: dict[str, str],
    public: list[str],
    grants: list[Grant],
    replace: bool = False,
    log=print,
) -> dict[str, int]:
    """Create ``params.database`` from ``source``; returns copied row counts per table."""
    target = params.database
    src = quote_identifier("source database", source)
    tgt = quote_identifier("database", target)
    studies = list(gated) + list(public)
    if target == source:
        raise ValueError("target database must differ from the source")
    if not studies or set(gated) & set(public):
        raise ValueError("pass at least one study, and no study as both gated and public")
    for study in studies:
        validate_name("study id", study)
    for group in gated.values():
        validate_name("private group", group)
        if group.upper() == params.public_group.upper():
            raise ValueError(f"private group {group} equals the public group")

    found = {
        r[0]
        for r in client.query(
            f"SELECT cancer_study_identifier FROM {src}.cancer_study "
            "WHERE cancer_study_identifier IN {ids:Array(String)}",
            parameters={"ids": studies},
        ).result_rows
    }
    missing = sorted(set(studies) - found)
    if missing:
        raise ValueError(f"studies not in {source}.cancer_study: {missing}")

    existing = client.query(
        "SELECT comment FROM system.databases WHERE name = {db:String}", parameters={"db": target}
    ).result_rows
    if existing:
        if not replace or existing[0][0] != MARKER:
            raise ValueError(
                f"{target} exists; pass --replace (only works on databases this tool created)"
            )
        client.command(f"DROP DATABASE {tgt} SYNC")
    client.command(f"CREATE DATABASE {tgt} COMMENT {sql_string(MARKER)}")

    objects = client.query(
        "SELECT name, engine, create_table_query FROM system.tables "
        "WHERE database = {db:String} AND NOT is_temporary ORDER BY name",
        parameters={"db": source},
    ).result_rows
    leaf = _in_list(studies)
    groups_expr = (
        "transform(assumeNotNull(cancer_study_identifier), "
        f"[{', '.join(sql_string(s) for s in gated)}]::Array(String), "
        f"[{', '.join(sql_string(g) for g in gated.values())}]::Array(String), "
        f"{sql_string(params.public_group)})"
    )
    counts: dict[str, int] = {}
    views = []
    for name, engine, create_query in objects:
        if engine == "View":
            views.append((name, create_query))
            continue
        if engine in {"MaterializedView", "Dictionary", "LiveView", "WindowView"}:
            log(f"skip   {name} ({engine})")
            continue
        if name in STUDY_SCOPED_TABLE_PATHS:
            where = render_path(STUDY_SCOPED_TABLE_PATHS[name], leaf, db=src)
            select = f"* REPLACE ({groups_expr} AS `groups`)" if name == "cancer_study" else "*"
        elif name in STUDY_AGNOSTIC_REFERENCE_TABLES:
            where, select = "1", "*"
        else:
            log(f"skip   {name} (auth/unclassified table: not copied)")
            continue
        table = quote_identifier("table", name)
        client.command(f"CREATE TABLE {tgt}.{table} AS {src}.{table}")
        client.command(
            f"INSERT INTO {tgt}.{table} SELECT {select} FROM {src}.{table} WHERE {where}"
        )
        counts[name] = client.query(f"SELECT count() FROM {tgt}.{table}").result_rows[0][0]
        log(f"copy   {name}: {counts[name]} rows")
    for name, create_query in views:
        if name not in STUDY_SCOPED_VIEWS:
            log(f"skip   view {name} (not in the catalog)")
            continue
        client.command(_rewrite_view(create_query, source, target))
        log(f"view   {name}")

    apply(client, params, log=log)
    if grants:
        client.insert(
            USER_STUDY_MANUAL_TABLE,
            [[g.email, g.grant_key, g.valid_until, "make-test-db"] for g in grants],
            database=target,
            column_names=["email", "grant_key", "valid_until", "note"],
        )
    client.command(f"SYSTEM REFRESH VIEW {tgt}.{USER_STUDY_REFRESH_VIEW}")
    client.command(f"SYSTEM WAIT VIEW {tgt}.{USER_STUDY_REFRESH_VIEW}")
    log(f"seeded {len(grants)} manual grants; user_study refreshed")
    return counts
