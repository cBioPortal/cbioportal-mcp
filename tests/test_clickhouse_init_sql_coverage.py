"""Guards against the local e2e ClickHouse mock schema drifting out of sync
with the study-authorization guard.

Fail-closed row security here relies on two things holding together in
``clickhouse-init.sql``:

1. A database-wide `GRANT SELECT ON db.*` - required because the server's own
   startup check (``ensure_db_permissions``) does `CHECK GRANT SELECT ON
   db.*`, which per-table grants do not satisfy (verified against ClickHouse
   24.12: granting SELECT on every existing table individually still leaves
   that check false).
2. A database-wide default-deny ROW POLICY (`ON db.*`, `USING 0`), which -
   verified live - automatically covers tables created *after* the policy,
   the same way the wildcard GRANT does. Multiple PERMISSIVE row policies on
   one table combine with OR, so a table with no policy of its own is stuck
   at `0` (nothing visible), while a table with its own policy gets
   `0 OR <real condition>` = `<real condition>`.

If a table is added to ``clickhouse-init.sql`` without its own row policy, or
without an entry in ``PROTECTED_QUERY_MARKERS`` / ``STUDY_AGNOSTIC_REFERENCE_TABLES``,
these tests fail loudly - even though the default-deny fallback keeps it from
leaking, an unrecognized table should never ship silently.
"""

import re
from pathlib import Path

from cbioportal_mcp.authentication.study_access import (
    PROTECTED_QUERY_MARKERS,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
)

INIT_SQL_PATH = (
    Path(__file__).resolve().parent.parent / "docker" / "local-e2e" / "clickhouse-init.sql"
)
DB = "cbioportal_authz_e2e"


def _init_sql() -> str:
    """Read clickhouse-init.sql with `--` line comments stripped.

    Comments are allowed to reference SQL shapes (e.g. explaining what NOT to
    write) without tripping the checks below, which only care about executed
    statements.
    """
    raw = INIT_SQL_PATH.read_text(encoding="utf-8")
    return re.sub(r"--[^\n]*", "", raw)


def _created_tables(sql: str) -> set[str]:
    return set(
        re.findall(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(DB)}\.([a-zA-Z0-9_]+)",
            sql,
        )
    )


def _row_policy_tables(sql: str) -> set[str]:
    return set(
        re.findall(
            rf"CREATE ROW POLICY IF NOT EXISTS \S+\s+ON {re.escape(DB)}\.([a-zA-Z0-9_]+)",
            sql,
        )
    )


def test_wildcard_select_grant_present():
    sql = _init_sql()
    assert re.search(rf"GRANT SELECT ON {re.escape(DB)}\.\*\s+TO", sql), (
        "Expected a database-wide `GRANT SELECT ON db.* TO "
        "cbioportal_mcp_study_restricted`. The server's own startup "
        "permission check (ensure_db_permissions) requires this exact grant "
        "shape - per-table grants do not satisfy `CHECK GRANT SELECT ON "
        "db.*`, so removing this in favor of per-table grants breaks "
        "startup rather than tightening security."
    )


def test_default_deny_wildcard_row_policy_present():
    sql = _init_sql()
    assert re.search(
        rf"CREATE ROW POLICY IF NOT EXISTS \S+\s+ON {re.escape(DB)}\.\*\s+USING 0\s+TO",
        sql,
    ), (
        "Expected a default-deny ROW POLICY on `db.*` (`USING 0`). This is "
        "what makes an unrecognized/forgotten table fail closed: it "
        "auto-covers tables created after it, and combines via OR with any "
        "table-specific policy, so a table with no policy of its own is "
        "stuck at `0` (no rows visible) instead of returning everything."
    )


def test_every_table_has_its_own_row_policy():
    sql = _init_sql()
    tables = _created_tables(sql)
    assert tables, "Expected to find CREATE TABLE statements in clickhouse-init.sql"

    policy_tables = _row_policy_tables(sql)
    missing_policy = sorted(tables - policy_tables)
    assert not missing_policy, (
        f"Tables with no table-specific ClickHouse ROW POLICY: {missing_policy}. "
        "The default-deny wildcard policy keeps these safe (zero rows), but "
        "that just makes them permanently unreadable rather than correctly "
        "scoped. Add a policy - a direct cancer_study_identifier check, or "
        "(for tables without that column) a subquery joining back to a "
        "table that has one, e.g. via sample_id/patient_id."
    )


def test_every_table_is_classified_in_python_guard():
    tables = _created_tables(_init_sql())
    unclassified = sorted(
        table
        for table in tables
        if table not in PROTECTED_QUERY_MARKERS and table not in STUDY_AGNOSTIC_REFERENCE_TABLES
    )
    assert not unclassified, (
        f"Tables not classified in study_access.py: {unclassified}. Add each "
        "to PROTECTED_QUERY_MARKERS (study-scoped - the app-level guard must "
        "require a literal study filter when row policies are disabled) or "
        "to STUDY_AGNOSTIC_REFERENCE_TABLES (reference data identical across "
        "every study, safe to leave unrestricted)."
    )
