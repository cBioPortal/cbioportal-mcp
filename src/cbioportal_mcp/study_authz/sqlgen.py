"""Generate and render the study-authz SQL.

``templates/1-row-policies.sql.tmpl`` is generated from ``catalog.py`` by
``row_policies_template()`` (``python -m cbioportal_mcp.study_authz generate``;
``tests/test_study_authz_sql.py`` fails on drift).
``templates/0-user-study.sql.tmpl`` is hand-written. Both keep placeholders
that ``render()`` fills in per database, so the same files serve blue and
green deployments.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cbioportal_mcp.study_authz.catalog import (
    DEFAULT_DENY_POLICY,
    DENIED_TABLES,
    ROW_POLICY_ROLE,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLE_PATHS,
    STUDY_SCOPED_VIEWS,
    UNRESTRICTED_OTHERS_POLICY,
    USER_STUDY_TABLE,
    AllOf,
    StudyId,
    StudyIdentifier,
    StudyPath,
    Via,
    describe,
)
from cbioportal_mcp.study_authz.groups import validate_name

TEMPLATES = Path(__file__).resolve().parent / "templates"
USER_STUDY_TEMPLATE = TEMPLATES / "0-user-study.sql.tmpl"
ROW_POLICIES_TEMPLATE = TEMPLATES / "1-row-policies.sql.tmpl"

DB = "__CLICKHOUSE_DATABASE__"
# The role or user every policy is attached TO (Params.policy_target).
TARGET = "__POLICY_TARGET__"
USER_SETTING = "SQL_cbio_user"
# The caller's own rows plus the public ('') rows, while still valid. A
# missing setting reads as '' (public only). Nothing relies on a
# settings-profile default for SQL_cbio_user (docs/study-authz.md).
USER_STUDY_MATCH = (
    f"(email = '' OR email = lowerUTF8(getSettingOrDefault('{USER_SETTING}', ''))) "
    "AND valid_until > now64(3)"
)
_PAD = "    "
_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _subquery(column: str, key: str, table: str, where: str, depth: int, db: str) -> str:
    inner = _PAD * (depth + 1)
    return (
        f"{column} IN (\n{inner}SELECT {key}\n{inner}FROM {db}.{table}\n"
        f"{inner}WHERE {where}\n{_PAD * depth})"
    )


def render_path(
    path: StudyPath, leaf: Callable[[str, int], str], db: str = DB, depth: int = 1
) -> str:
    """Boolean condition that ``path`` reaches an allowed study.

    ``leaf(column, depth)`` renders the condition on a column holding
    cancer_study_identifier; numeric cancer_study_id columns go through
    cancer_study. Subqueries repeat the full FK path instead of relying on the
    parent table's own policy, so each condition is correct on its own (and
    usable by the test-database copier, where no policies apply).
    """
    if isinstance(path, StudyIdentifier):
        return leaf(path.column, depth)
    if isinstance(path, StudyId):
        where = leaf("cancer_study_identifier", depth + 1)
        return _subquery(path.column, "cancer_study_id", "cancer_study", where, depth, db)
    if isinstance(path, Via):
        where = render_path(path.then, leaf, db, depth + 1)
        return _subquery(path.column, path.key, path.parent, where, depth, db)
    if isinstance(path, AllOf):
        inner = _PAD * (depth + 1)
        parts = f"\n{inner}AND ".join(render_path(p, leaf, db, depth + 1) for p in path.paths)
        return f"(\n{inner}{parts}\n{_PAD * depth})"
    raise TypeError(f"unknown study path {path!r}")


def _user_study_leaf(column: str, depth: int) -> str:
    return _subquery(
        column, "cancer_study_identifier", USER_STUDY_TABLE, USER_STUDY_MATCH, depth, DB
    )


def study_condition(table: str) -> str:
    return render_path(STUDY_SCOPED_TABLE_PATHS[table], _user_study_leaf)


def _policy(name: str, table: str, using: str) -> str:
    return (
        f"CREATE ROW POLICY OR REPLACE {name}\nON {DB}.{table}\nAS PERMISSIVE FOR SELECT\n"
        f"USING {using}\nTO {TARGET};"
    )


def _one_line_policy(name: str, table: str) -> str:
    return (
        f"CREATE ROW POLICY OR REPLACE {name} ON {DB}.{table} "
        f"AS PERMISSIVE FOR SELECT USING 1 TO {TARGET};"
    )


_HEADER = f"""\
-- ============================================================================
-- study-authz 1: row policies TO {TARGET} (a role or a user)
-- ============================================================================
-- GENERATED from src/cbioportal_mcp/study_authz/catalog.py by
-- `python -m cbioportal_mcp.study_authz generate` - do not edit by hand;
-- tests/test_study_authz_sql.py fails on drift. Apply after 0-user-study.sql.tmpl
-- with `python -m cbioportal_mcp.study_authz apply` (docs/study-authz.md).
--
-- Every study row resolves to its cancer_study_identifier and is visible iff
-- that study is in
--   SELECT cancer_study_identifier FROM {DB}.user_study
--   WHERE {USER_STUDY_MATCH}
-- i.e. the caller's own grants plus the public ('') rows. The MCP sends the
-- verified, lower-cased caller email as the per-query setting {USER_SETTING}
-- together with readonly=1, which rejects any SETTINGS {USER_SETTING}=... the
-- query itself tries. A missing or empty setting sees public studies only.
--
-- Requires ClickHouse >= 24.10 (getSettingOrDefault) and the SQL_ custom
-- settings prefix (the server default). Every reference is qualified with the
-- database: an unqualified name in a policy subquery resolves against the
-- querying session's database. Do not grant the target CREATE TEMPORARY
-- TABLE, REMOTE / table functions, dictGet, or DEFINER views.
--
-- Policy target (TO): {TARGET}. Either the role {ROW_POLICY_ROLE} (apply
-- prepends CREATE ROLE) or an existing user. Role-targeted: the runtime user
-- must get SELECT only through the role (a direct grant + SET ROLE NONE
-- bypasses every policy).
-- User-targeted: the policies follow the user whatever its grants or roles.
--
-- Order matters: policies first, the SELECT grant last, so the target never
-- reads an unfiltered table. Idempotent (CREATE ... OR REPLACE / IF NOT
-- EXISTS). Rollback: DROP ROW POLICY IF EXISTS <name> ON {DB}.<table> for every
-- row of system.row_policies WHERE name LIKE 'cbioportal_mcp_%'.
-- ============================================================================

-- 1. Database-wide fallbacks. Permissive policies on one table are OR-ed, so a
-- table with no policy of its own stays at 0 (nothing visible): a table added
-- later, or forgotten here, fails closed. Everyone except the target (admin,
-- the refresh view's definer) keeps full access even where
-- users_without_row_policies_can_read_rows = false.
CREATE ROW POLICY OR REPLACE {UNRESTRICTED_OTHERS_POLICY}
ON {DB}.*
AS PERMISSIVE FOR SELECT
USING 1
TO ALL EXCEPT {TARGET};

CREATE ROW POLICY OR REPLACE {DEFAULT_DENY_POLICY}
ON {DB}.*
AS PERMISSIVE FOR SELECT
USING 0
TO {TARGET};

-- 2. user_study: the caller's own rows and the public ones. Every study policy
-- below reads user_study in a subquery as the caller, so this policy also keeps
-- one caller from listing another's grants.
{_policy("cbioportal_mcp_identity_user_study", USER_STUDY_TABLE, USER_STUDY_MATCH)}
"""

_VIEWS_NOTE = """\
-- 5. Views (sql/4, sql/5, sql/7): pass-through. The default-deny matches views
-- too. USING 1 on a view is safe only because every view is SQL SECURITY
-- INVOKER (sql/README.md), so the underlying tables' policies still filter the
-- rows; `python -m cbioportal_mcp.study_authz check` refuses DEFINER/NONE views."""


def row_policies_template() -> str:
    """The complete generated policy template (placeholder ``__CLICKHOUSE_DATABASE__``)."""
    out = [_HEADER.rstrip(), "", "-- 3. Study-scoped tables (catalog.STUDY_SCOPED_TABLE_PATHS)."]
    for table in sorted(STUDY_SCOPED_TABLE_PATHS):
        path = STUDY_SCOPED_TABLE_PATHS[table]
        out += ["", f"-- {table}: {describe(path)}"]
        out.append(_policy(f"cbioportal_mcp_study_{table}", table, study_condition(table)))
    out += ["", "-- 4. Study-agnostic reference data: readable by everyone (USING 1)."]
    out += [
        _one_line_policy(f"cbioportal_mcp_reference_{t}", t)
        for t in sorted(STUDY_AGNOSTIC_REFERENCE_TABLES)
    ]
    out += ["", _VIEWS_NOTE]
    out += [_one_line_policy(f"cbioportal_mcp_view_{v}", v) for v in sorted(STUDY_SCOPED_VIEWS)]
    out += [
        "",
        f"-- 6. Deliberately denied (no policy, default-deny): {', '.join(sorted(DENIED_TABLES))}.",
        "",
        "-- 7. Only now let the target read the database. With a role target, give the",
        "-- MCP user the role via `python -m cbioportal_mcp.study_authz user-sql` (role",
        "-- only, no direct grants: a direct SELECT plus SET ROLE NONE bypasses policies).",
        "-- SHOW ROW POLICIES lets the MCP's startup check (study filtering on) verify",
        "-- that these policies apply to its own user; policy text holds no emails.",
        f"GRANT SHOW ROW POLICIES ON {DB}.* TO {TARGET};",
        f"GRANT SELECT ON {DB}.* TO {TARGET};",
        "",
    ]
    return "\n".join(out)


@dataclass(frozen=True)
class Params:
    """Per-deployment values substituted into the templates."""

    database: str
    public_group: str
    app_name: str
    refresh_minutes: int = 5
    private_grace_minutes: int = 15
    # Who the policies apply TO: the restricted role (default), or one user.
    policy_target: str = ROW_POLICY_ROLE
    target_is_user: bool = False

    def __post_init__(self):
        if not _DB_RE.match(self.database or ""):
            raise ValueError(f"database must match {_DB_RE.pattern}, got {self.database!r}")
        validate_name("public group", self.public_group)
        validate_name("app name", self.app_name)
        if not _DB_RE.match(self.policy_target or ""):
            raise ValueError(f"policy target must match {_DB_RE.pattern}")
        if not 1 <= self.refresh_minutes <= 1440:
            raise ValueError("refresh_minutes must be in 1..1440")
        if not self.refresh_minutes < self.private_grace_minutes <= 7 * 1440:
            raise ValueError(
                "private_grace_minutes must exceed refresh_minutes (else private grants "
                "lapse between refreshes) and be at most 7 days"
            )


def render(template: str, params: Params) -> str:
    rendered = (
        template.replace(DB, params.database)
        .replace("__PUBLIC_GROUP__", params.public_group)
        .replace("__APP_NAME__", params.app_name)
        .replace("__REFRESH_MINUTES__", str(params.refresh_minutes))
        .replace("__PRIVATE_GRACE_MINUTES__", str(params.private_grace_minutes))
        .replace(TARGET, params.policy_target)
    )
    leftover = re.findall(r"__[A-Z_]+__", rendered)
    if leftover:
        raise ValueError(f"unrendered placeholders: {sorted(set(leftover))}")
    return rendered


def rendered_files(params: Params) -> list[tuple[str, str]]:
    """(name, SQL) in apply order. A role target is created first; a user target
    must already exist (apply checks)."""
    policies = render(ROW_POLICIES_TEMPLATE.read_text(), params)
    if not params.target_is_user:
        policies = f"CREATE ROLE IF NOT EXISTS {params.policy_target};\n\n{policies}"
    return [
        (USER_STUDY_TEMPLATE.name, render(USER_STUDY_TEMPLATE.read_text(), params)),
        (ROW_POLICIES_TEMPLATE.name, policies),
    ]


def statements(sql: str) -> list[str]:
    """Split a rendered file into statements.

    The templates end every statement with ';' at the end of a line and never
    end a line inside a string literal with one, so this is exact for them.
    """
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in re.split(r";[ \t]*$", body, flags=re.M) if s.strip()]


def apply(client, params: Params, log=print) -> None:
    """Run both rendered files, statement by statement, with an admin client."""
    if (
        params.target_is_user
        and not client.query(
            "SELECT 1 FROM system.users WHERE name = {u:String}",
            parameters={"u": params.policy_target},
        ).result_rows
    ):
        raise ValueError(f"policy target user {params.policy_target} does not exist")
    for name, sql in rendered_files(params):
        log(f"apply  {name} -> {params.database}")
        for statement in statements(sql):
            client.command(statement)


def restricted_user_sql(user: str, password: str, database: str) -> str:
    """SQL creating an MCP runtime user that reads ONLY through the role.

    CREATE USER OR REPLACE drops any direct grant the user had. No settings
    profile is attached: the MCP must be able to send SQL_cbio_user and
    readonly=1 per query, which a profile forcing readonly would forbid.
    """
    if not _DB_RE.match(user or "") or not _DB_RE.match(database or ""):
        raise ValueError("user and database must match ^[A-Za-z0-9_]+$")
    if not password:
        raise ValueError("password must not be empty")
    digest = hashlib.sha256(password.encode()).hexdigest()
    return "\n".join(
        [
            f"CREATE USER OR REPLACE {user} IDENTIFIED WITH sha256_hash BY '{digest}'",
            f"    DEFAULT DATABASE {database};",
            f"GRANT {ROW_POLICY_ROLE} TO {user};",
            f"SET DEFAULT ROLE {ROW_POLICY_ROLE} TO {user};",
            "",
        ]
    )
