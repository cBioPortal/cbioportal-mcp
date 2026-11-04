"""Generate the study row-policy SQL from row_policy_catalog.

The catalog is the single source of truth: every policy in
``sql/study-authz/0-row-policies.sql`` (``acl`` source) and
``sql/study-authz/authorities/1-row-policies.sql`` (``authorities`` source) is
rendered from ``STUDY_SCOPED_TABLE_PATHS``, ``STUDY_AGNOSTIC_REFERENCE_TABLES``,
``STUDY_SCOPED_VIEWS`` and ``IDENTITY_SCOPED_TABLES``. The checked-in files are
this module's output (``scripts/generate_row_policy_sql.py`` writes them,
``tests/test_row_policy_sql.py`` fails on drift), so the apply scripts stay
plain shell + clickhouse-client.

The files keep placeholders that the apply scripts substitute for the target
deployment, so the SQL always matches what the server sends:

- ``__CLICKHOUSE_DATABASE__``: the target database (every reference is
  qualified; an unqualified name inside a policy subquery resolves against
  the *querying* session's database).
- ``__CBIOMCP_ALLOWED_STUDIES_SETTING__``: ``acl`` source; the value of
  ``CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING``.
- ``__CBIOMCP_USER_SETTING__``: ``authorities`` source; the value of
  ``CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING``.
- ``__CBIOMCP_MAX_STALENESS_SECONDS__``: ``authorities`` source; how old a
  user_study refresh may be before its rows stop granting anything
  (``STUDY_AUTHZ_MAX_STALENESS_SECONDS``, default 2x the refresh interval).
"""

from __future__ import annotations

from cbioportal_mcp.authentication.row_policy_catalog import (
    DEFAULT_DENY_POLICY,
    DENIED_TABLES,
    IDENTITY_SCOPED_TABLES,
    ROW_POLICY_ROLE,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLE_PATHS,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
    UNRESTRICTED_OTHERS_POLICY,
    USER_STUDY_TABLE,
    AllOf,
    StudyId,
    StudyIdentifier,
    StudyPath,
    Via,
)

DB = "__CLICKHOUSE_DATABASE__"
ALLOWED_STUDIES_SETTING_PLACEHOLDER = "__CBIOMCP_ALLOWED_STUDIES_SETTING__"
USER_SETTING_PLACEHOLDER = "__CBIOMCP_USER_SETTING__"
MAX_STALENESS_PLACEHOLDER = "__CBIOMCP_MAX_STALENESS_SECONDS__"

SOURCE_ACL = "acl"
SOURCE_AUTHORITIES = "authorities"
SOURCES = (SOURCE_ACL, SOURCE_AUTHORITIES)

_PAD = "    "


def _acl_has(column: str) -> str:
    return f"has(splitByChar(',', getSetting('{ALLOWED_STUDIES_SETTING_PLACEHOLDER}')), {column})"


def _user_match() -> str:
    """The caller's own user_study rows, and only while they are fresh.

    refreshed_at is written by every user_study refresh; once the last
    successful refresh is older than the bound (refreshes stopped or
    failing), no row matches and every study-scoped table returns nothing -
    revocations can't be outlived by a broken refresh.
    """
    return (
        f"email = getSetting('{USER_SETTING_PLACEHOLDER}') "
        f"AND refreshed_at >= now() - toIntervalSecond({MAX_STALENESS_PLACEHOLDER})"
    )


def _subquery(column: str, key: str, table: str, where: str, depth: int) -> str:
    inner = _PAD * (depth + 1)
    return (
        f"{column} IN (\n"
        f"{inner}SELECT {key}\n"
        f"{inner}FROM {DB}.{table}\n"
        f"{inner}WHERE {where}\n"
        f"{_PAD * depth})"
    )


def render_path(path: StudyPath, source: str, depth: int = 1) -> str:
    """The boolean condition that ``path`` leads to an allowed study.

    ``acl``: the study identifier is in the comma-separated allowlist setting.
    ``authorities``: the study is in ``user_study`` for the caller's email;
    numeric cancer_study_id paths use ``user_study.cancer_study_id`` directly
    instead of going through cancer_study.
    """
    if isinstance(path, StudyIdentifier):
        if source == SOURCE_ACL:
            return _acl_has(path.column)
        return _subquery(
            path.column, "cancer_study_identifier", USER_STUDY_TABLE, _user_match(), depth
        )
    if isinstance(path, StudyId):
        if source == SOURCE_ACL:
            where = _acl_has("cancer_study_identifier")
            return _subquery(path.column, "cancer_study_id", "cancer_study", where, depth)
        return _subquery(path.column, "cancer_study_id", USER_STUDY_TABLE, _user_match(), depth)
    if isinstance(path, Via):
        where = render_path(path.then, source, depth + 1)
        return _subquery(path.column, path.key, path.parent, where, depth)
    if isinstance(path, AllOf):
        inner = _PAD * (depth + 1)
        parts = f"\n{inner}AND ".join(render_path(p, source, depth + 1) for p in path.paths)
        return f"(\n{inner}{parts}\n{_PAD * depth})"
    raise TypeError(f"unknown study path {path!r}")


def study_condition(table: str, source: str) -> str:
    """The full USING expression for a study-scoped table."""
    condition = render_path(STUDY_SCOPED_TABLE_PATHS[table], source)
    if source == SOURCE_ACL:
        # '*' short-circuit: unrestricted callers skip the subqueries.
        setting = f"getSetting('{ALLOWED_STUDIES_SETTING_PLACEHOLDER}')"
        return f"{setting} = '*'\n{_PAD}OR {condition}"
    # authorities: no unrestricted value exists. A missing setting makes
    # getSetting() raise; an empty one matches no user_study row.
    return condition


def _policy(name: str, table: str, using: str) -> str:
    return (
        f"CREATE ROW POLICY OR REPLACE {name}\n"
        f"ON {DB}.{table}\n"
        "AS PERMISSIVE FOR SELECT\n"
        f"USING {using}\n"
        f"TO {ROW_POLICY_ROLE};\n"
    )


def _one_line_policy(name: str, table: str) -> str:
    return (
        f"CREATE ROW POLICY OR REPLACE {name} ON {DB}.{table} "
        f"AS PERMISSIVE FOR SELECT USING 1 TO {ROW_POLICY_ROLE};"
    )


_HEADER = {
    SOURCE_ACL: """\
-- ============================================================================
-- Study-level row policies: ACL source (CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=acl)
-- ============================================================================
-- GENERATED by scripts/generate_row_policy_sql.py from
-- src/cbioportal_mcp/authentication/row_policy_catalog.py - do not edit by
-- hand; tests/test_row_policy_sql.py fails if this file drifts.
--
-- OPT-IN. Not part of the default sql/*.sql run: apply_sql.sh applies it only
-- with APPLY_STUDY_AUTHZ_SQL=true (see sql/README.md).
--
-- The MCP server (CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true) sends the
-- caller's allowed studies on every SELECT as the custom setting
-- __CBIOMCP_ALLOWED_STUDIES_SETTING__ (rendered from
-- CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING): '*' = every study,
-- otherwise a comma-separated list of cancer_study_identifier values ('' =
-- none). If the setting is missing, getSetting() raises, so a query that
-- bypasses the MCP fails instead of returning rows.
""",
    SOURCE_AUTHORITIES: """\
-- ============================================================================
-- Study-level row policies: authorities source
-- (CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities)
-- ============================================================================
-- GENERATED by scripts/generate_row_policy_sql.py from
-- src/cbioportal_mcp/authentication/row_policy_catalog.py - do not edit by
-- hand; tests/test_row_policy_sql.py fails if this file drifts.
--
-- OPT-IN. apply_sql.sh applies it with APPLY_STUDY_AUTHZ_SQL=true and
-- STUDY_AUTHZ_SOURCE=authorities, after 0-user-study.sql has created
-- __CLICKHOUSE_DATABASE__.user_study (see sql/README.md).
--
-- The MCP server sends only the caller's VERIFIED, lower-cased email as the
-- custom setting __CBIOMCP_USER_SETTING__ (rendered from
-- CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING). ClickHouse decides which studies
-- that is: every policy resolves the row's study through user_study, which
-- 0-user-study.sql rebuilds from cBioPortal's authorities/users/cancer_study.
-- There is no unrestricted value: an empty setting matches no user_study row
-- (zero rows everywhere), and a missing one makes getSetting() raise.
-- Freshness: every user_study match also requires
-- refreshed_at >= now() - __CBIOMCP_MAX_STALENESS_SECONDS__ seconds
-- (STUDY_AUTHZ_MAX_STALENESS_SECONDS), so if the refresh stops or fails for
-- longer than that, every caller sees nothing until it succeeds again.
""",
}

_COMMON_PRELUDE = f"""\
--
-- Every table reference is qualified with {DB}, which the
-- apply scripts replace with the target database. Unqualified names inside a
-- policy's subquery would be resolved against the *querying session's*
-- current database (verified on ClickHouse 24.12), not the policy's.
--
-- Requires: <custom_settings_prefixes> including SQL_ (the ClickHouse default)
-- and an admin user allowed to CREATE ROLE / CREATE ROW POLICY. Do not grant
-- the restricted role CREATE TEMPORARY TABLE or REMOTE (they enable table
-- functions such as merge() / remote()), dictGet, or SQL SECURITY DEFINER
-- views.
--
-- Idempotent: CREATE ... IF NOT EXISTS / CREATE ROW POLICY OR REPLACE.
-- Rollback: DROP ROW POLICY IF EXISTS <name> ON {DB}.<table>
-- for each policy below (SELECT name FROM system.row_policies
-- WHERE name LIKE 'cbioportal_mcp_%').
-- ============================================================================

CREATE ROLE IF NOT EXISTS {ROW_POLICY_ROLE};

-- Database-wide grant: the MCP's startup check requires SELECT ON <db>.*, and
-- schema discovery (SHOW TABLES) is grant-scoped. Row filtering is entirely
-- the policies' job. Grant the role to the MCP runtime user separately:
--   GRANT {ROW_POLICY_ROLE} TO <mcp_user>;
--   SET DEFAULT ROLE {ROW_POLICY_ROLE} TO <mcp_user>;
GRANT SELECT ON {DB}.* TO {ROW_POLICY_ROLE};

-- ============================================================================
-- 1. Database-wide fallbacks.
-- ============================================================================
-- Default-deny: PERMISSIVE policies on one table combine with OR, so a table
-- with no policy of its own stays at 0 (nothing visible) and a table with one
-- gets `0 OR <condition>`. A table added later - or forgotten here - fails
-- closed. scripts/verify_row_policy_coverage.py flags study-scoped tables
-- that only have this fallback (fully denied by accident).
CREATE ROW POLICY OR REPLACE {DEFAULT_DENY_POLICY}
ON {DB}.*
AS PERMISSIVE FOR SELECT
USING 0
TO {ROW_POLICY_ROLE};

-- Everyone else (admin, the public MCP user, the clone job) keeps full access
-- even on servers where users_without_row_policies_can_read_rows = false:
-- once a table has any row policy, ClickHouse otherwise hides its rows from
-- users the policies do not mention.
CREATE ROW POLICY OR REPLACE {UNRESTRICTED_OTHERS_POLICY}
ON {DB}.*
AS PERMISSIVE FOR SELECT
USING 1
TO ALL EXCEPT {ROW_POLICY_ROLE};
"""

_VIEWS_SECTION = """\
-- ============================================================================
-- 4. Views (sql/4, sql/5, sql/7): pass-through (USING 1).
-- ============================================================================
-- The database-wide default-deny in section 1 also matches views - without
-- these, every view returns zero rows for the restricted role (verified on
-- ClickHouse 24.12 and 25.8). USING 1 on the view itself is safe because the
-- view's query runs as the invoker, so every underlying table's own policy
-- above still filters the rows. That only holds for SQL SECURITY INVOKER: a
-- DEFINER view reads as its definer, and a NONE view runs without the
-- caller's row-policy context, so either would leak every study through its
-- USING 1 policy. sql/4, sql/5 and sql/7 declare SQL SECURITY INVOKER in every
-- CREATE VIEW, and the ALTERs below migrate views created before that (they
-- need the views to exist: apply this file after sql/4, sql/5 and sql/7). The
-- server's startup check and scripts/verify_row_policy_coverage.py refuse any
-- view that is not explicitly INVOKER.
"""


def _study_section(source: str) -> list[str]:
    out = [
        "",
        "-- ============================================================================",
        "-- 2. Study-scoped tables (row_policy_catalog.STUDY_SCOPED_TABLE_PATHS).",
        "-- ============================================================================",
    ]
    if source == SOURCE_ACL:
        out += [
            "-- Every condition starts with the '*' short-circuit so unrestricted callers",
            "-- skip the subqueries. Subqueries repeat the full path to cancer_study rather",
            "-- than relying on the parent table's own policy (which ClickHouse also",
            "-- applies inside the subquery), so each policy is correct on its own.",
        ]
    else:
        out += [
            "-- Paths that reach a numeric cancer_study_id end at user_study.cancer_study_id;",
            "-- identifier columns end at user_study.cancer_study_identifier. Subqueries",
            "-- repeat the full FK path rather than relying on the parent table's own",
            "-- policy, so each policy is correct on its own.",
        ]
    for table in sorted(STUDY_SCOPED_TABLE_PATHS):
        out.append("")
        out.append(f"-- {table}: {STUDY_SCOPED_TABLES[table]}")
        out.append(
            _policy(f"cbioportal_mcp_study_{table}", table, study_condition(table, source)).rstrip()
        )
    return out


def _identity_section() -> list[str]:
    out = [
        "",
        "-- ============================================================================",
        "-- 2b. Identity-scoped: user_study (row_policy_catalog.IDENTITY_SCOPED_TABLES).",
        "-- ============================================================================",
        "-- The restricted role can read user_study (SELECT ON <db>.*), and every policy",
        "-- above reads it in a subquery as that role - so this policy is what keeps a",
        "-- caller from listing other users' entitlements: only the rows for the",
        "-- caller's own verified email are visible, both to the caller's own SQL and",
        "-- inside the other policies' subqueries. Rows from a refresh older than",
        "-- __CBIOMCP_MAX_STALENESS_SECONDS__ seconds match nothing (fail closed when",
        "-- user_study_refresh stops or keeps failing).",
    ]
    for table, column in sorted(IDENTITY_SCOPED_TABLES.items()):
        assert column == "email", "identity policy is written for user_study.email"
        using = _user_match()
        out.append("")
        out.append(_policy(f"cbioportal_mcp_identity_{table}", table, using).rstrip())
    return out


def render_row_policy_sql(source: str) -> str:
    """The complete policy file for ``source`` ('acl' or 'authorities')."""
    if source not in SOURCES:
        raise ValueError(f"unknown study access source {source!r}; expected one of {SOURCES}")
    lines = [_HEADER[source] + _COMMON_PRELUDE.rstrip()]
    lines += _study_section(source)
    if source == SOURCE_AUTHORITIES:
        lines += _identity_section()
    lines += [
        "",
        "-- ============================================================================",
        "-- 3. Study-agnostic reference data: explicitly readable (USING 1).",
        "-- ============================================================================",
        "",
    ]
    lines += [
        _one_line_policy(f"cbioportal_mcp_reference_{t}", t)
        for t in sorted(STUDY_AGNOSTIC_REFERENCE_TABLES)
    ]
    lines += ["", _VIEWS_SECTION.rstrip(), ""]
    lines += [_one_line_policy(f"cbioportal_mcp_view_{v}", v) for v in sorted(STUDY_SCOPED_VIEWS)]
    lines.append("")
    lines += [
        f"ALTER TABLE {DB}.{v} MODIFY SQL SECURITY INVOKER;" for v in sorted(STUDY_SCOPED_VIEWS)
    ]
    denied = ", ".join(sorted(DENIED_TABLES))
    lines += [
        "",
        "-- ============================================================================",
        f"-- 5. Deliberately denied: {denied}.",
        "-- ============================================================================",
        "-- cBioPortal's own auth tables (and, in authorities mode, the refreshable",
        "-- materialized view that expands them) get no policy, so the default-deny in",
        "-- section 1 keeps them unreadable (zero rows) for the restricted role.",
        "",
    ]
    return "\n".join(lines)
