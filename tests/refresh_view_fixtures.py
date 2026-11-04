"""Stored-form user_study_refresh definitions for tests that have no ClickHouse.

The real check formats the expected MV with ClickHouse's formatQuerySingleLine
and compares it with system.tables.create_table_query (proven against a real
server in tests/test_user_study_clickhouse.py). Without a server, both sides
use the same stand-in formatter (whitespace collapsed), and the "stored" text
gets what ClickHouse adds when it stores the DDL: the target's column list and
the resolved definer.
"""

from __future__ import annotations

from cbioportal_mcp.authentication.study_access import _lex_sql
from cbioportal_mcp.authentication.user_study_sql import refresh_view_statement


# What ClickHouse writes for the TO target's columns: the SELECT's output
# structure, so cancer_study_id follows the source table's type (Int64
# upstream, Int32 in the local e2e fixture).
def target_columns(cancer_study_id_type: str = "Int64") -> str:
    return (
        "(`email` String, `cancer_study_identifier` String, "
        f"`cancer_study_id` {cancer_study_id_type}, `refreshed_at` DateTime)"
    )


_REFRESH_VIEW_TARGET_COLUMNS = target_columns()


def fake_format(sql: str) -> str:
    return " ".join(sql.split())


def fake_format_query_result(query: str) -> dict:
    """Answer a `SELECT formatQuerySingleLine('<sql>')` sent through run_query_with_settings."""
    (literal,) = [t.value for t in _lex_sql(query) if t.kind == "string"]
    return {"columns": ["q"], "rows": [[fake_format(literal)]]}


def stored_refresh_mv(
    db: str = "cbio",
    *,
    every: str = "5 MINUTE",
    app: str = "cbioportal",
    filter_by_app: str = "1",
    public: str = "PUBLIC",
    definer: str = "admin",
    columns: str | None = None,
) -> str:
    statement = (
        refresh_view_statement()
        .replace("__CLICKHOUSE_DATABASE__", db)
        .replace("__CBIOMCP_REFRESH_MINUTES__ MINUTE", every)
        .replace("__CBIOMCP_APP_NAME__", app)
        .replace("__CBIOMCP_FILTER_GROUPS_BY_APP_NAME__", filter_by_app)
        .replace("__CBIOMCP_PUBLIC_GROUP__", public)
    )
    return (
        fake_format(statement)
        .replace("DEFINER = CURRENT_USER", f"DEFINER = {definer}")
        .replace(
            f"TO {db}.user_study ",
            f"TO {db}.user_study {columns or _REFRESH_VIEW_TARGET_COLUMNS} ",
            1,
        )
    )


# The two look-alikes the round-2 review got past the previous validator.
CROSS_JOIN_LOOKALIKE = (
    "CREATE MATERIALIZED VIEW {db}.user_study_refresh REFRESH EVERY 5 MINUTE TO {db}.user_study "
    f"{_REFRESH_VIEW_TARGET_COLUMNS} DEFINER = admin SQL SECURITY DEFINER AS "
    "WITH known_users AS (SELECT lower(email) AS email FROM {db}.users) "
    "SELECT lower(u.email) AS email, s.cancer_study_identifier AS cancer_study_identifier, "
    "s.cancer_study_id AS cancer_study_id, now() + toIntervalYear(10) AS refreshed_at "
    "FROM {db}.users AS u CROSS JOIN {db}.cancer_study AS s "
    "WHERE u.email IN (SELECT email FROM known_users)"
)
COMMA_JOIN_LOOKALIKE = (
    "CREATE MATERIALIZED VIEW {db}.user_study_refresh REFRESH EVERY 5 MINUTE TO {db}.user_study "
    f"{_REFRESH_VIEW_TARGET_COLUMNS} DEFINER = admin SQL SECURITY DEFINER AS "
    "SELECT u.email AS email, x.s AS cancer_study_identifier, toInt64(0) AS cancer_study_id, "
    "now() AS refreshed_at FROM {db}.users AS u, secrets.entitlements AS x"
)
