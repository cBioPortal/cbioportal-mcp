"""Keeps sql/study-authz/0-row-policies.sql, row_policy_catalog.py, the
app-level guard, the repo's own sql/*.sql views and the local e2e fixture in
sync.

The live behaviour (rows actually filtered) is exercised by the local e2e
(docker/local-e2e + scripts/local_e2e_keycloak_authz.py); these tests catch
drift without needing ClickHouse: a table classified but never given a
policy, a view added to sql/4-7 with no pass-through policy (fully denied by
the database-wide default-deny), an unqualified table name in a policy
subquery (resolved against the querying session's database), and so on.
"""

import re
from pathlib import Path

import pytest

from cbioportal_mcp.authentication.row_policy_catalog import (
    DEFAULT_DENY_POLICY,
    DENIED_TABLES,
    ROW_POLICY_ROLE,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
    UNRESTRICTED_OTHERS_POLICY,
    classified_names,
)
from cbioportal_mcp.authentication.study_access import (
    PROTECTED_QUERY_MARKERS,
    references_protected_study_data,
)

REPO = Path(__file__).resolve().parent.parent
POLICY_SQL = REPO / "sql" / "study-authz" / "0-row-policies.sql"
FIXTURE_SQL = REPO / "docker" / "local-e2e" / "clickhouse-init.sql"
DB = "__CLICKHOUSE_DATABASE__"
SETTING = "getSetting('SQL_cbiomcp_allowed_studies')"


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _policies() -> dict[str, list[tuple[str, str, str]]]:
    """table ('*' for db-wide) -> [(policy name, USING expression, TO clause)]."""
    sql = _strip_comments(POLICY_SQL.read_text(encoding="utf-8"))
    pattern = re.compile(
        r"CREATE ROW POLICY OR REPLACE (\w+)\s+ON (\S+)\s+AS PERMISSIVE FOR SELECT\s+"
        r"USING (.*?)\s+TO (.*?);",
        re.S,
    )
    policies: dict[str, list[tuple[str, str, str]]] = {}
    for name, target, using, to in pattern.findall(sql):
        assert target.startswith(f"{DB}."), f"{name}: ON {target} is not qualified with {DB}"
        table = target[len(DB) + 1 :]
        policies.setdefault(table, []).append((name, " ".join(using.split()), to.strip()))
    statement_count = len(re.findall(r"CREATE ROW POLICY", sql))
    assert statement_count == sum(
        len(v) for v in policies.values()
    ), "a CREATE ROW POLICY statement did not match the expected shape"
    return policies


def _repo_sql_views() -> set[str]:
    views: set[str] = set()
    for path in sorted((REPO / "sql").glob("*.sql")):
        sql = _strip_comments(path.read_text(encoding="utf-8"))
        views.update(re.findall(r"CREATE (?:OR REPLACE )?VIEW (?:IF NOT EXISTS )?(\w+)", sql))
    return views


def _fixture_tables() -> dict[str, str]:
    sql = _strip_comments(FIXTURE_SQL.read_text(encoding="utf-8"))
    pattern = r"CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e\.(\w+)\s*\((.*?)\)\s*ENGINE"
    return dict(re.findall(pattern, sql, re.S))


def test_catalog_buckets_are_disjoint():
    buckets = [
        set(STUDY_SCOPED_TABLES),
        set(STUDY_AGNOSTIC_REFERENCE_TABLES),
        set(DENIED_TABLES),
        set(STUDY_SCOPED_VIEWS),
    ]
    total = sum(len(bucket) for bucket in buckets)
    assert total == len(classified_names()), "a name is classified in more than one bucket"


def test_default_deny_and_unrestricted_others_fallbacks():
    wildcard = _policies()["*"]
    by_name = {name: (using, to) for name, using, to in wildcard}
    assert by_name[DEFAULT_DENY_POLICY] == ("0", ROW_POLICY_ROLE)
    assert by_name[UNRESTRICTED_OTHERS_POLICY] == ("1", f"ALL EXCEPT {ROW_POLICY_ROLE}")
    assert set(by_name) == {DEFAULT_DENY_POLICY, UNRESTRICTED_OTHERS_POLICY}


def test_wildcard_select_grant_and_role():
    sql = _strip_comments(POLICY_SQL.read_text(encoding="utf-8"))
    assert f"CREATE ROLE IF NOT EXISTS {ROW_POLICY_ROLE};" in sql
    # ensure_db_permissions requires the database-wide grant shape.
    assert f"GRANT SELECT ON {DB}.* TO {ROW_POLICY_ROLE};" in sql


def test_every_policy_targets_only_the_restricted_role():
    for table, entries in _policies().items():
        for name, _using, to in entries:
            if name == UNRESTRICTED_OTHERS_POLICY:
                continue
            assert to == ROW_POLICY_ROLE, (table, name, to)


@pytest.mark.parametrize("table", sorted(STUDY_SCOPED_TABLES))
def test_study_scoped_table_has_one_filtering_policy(table):
    entries = _policies().get(table, [])
    assert len(entries) == 1, f"{table}: expected exactly one policy, got {entries}"
    _name, using, _to = entries[0]
    assert using.startswith(f"{SETTING} = '*' OR "), using
    assert f"has(splitByChar(',', {SETTING})," in using, using
    assert using not in {"0", "1"}


@pytest.mark.parametrize("table", sorted(STUDY_AGNOSTIC_REFERENCE_TABLES | STUDY_SCOPED_VIEWS))
def test_reference_tables_and_views_pass_through(table):
    entries = _policies().get(table, [])
    assert [using for _name, using, _to in entries] == ["1"], (table, entries)


def test_denied_tables_have_no_policy():
    policies = _policies()
    assert not DENIED_TABLES & set(policies)


def test_no_policy_for_unclassified_tables():
    unknown = set(_policies()) - classified_names() - {"*"}
    assert not unknown, f"policies for tables missing from row_policy_catalog: {sorted(unknown)}"


def test_policy_subqueries_are_fully_qualified():
    # Unqualified names in a policy's subquery resolve against the querying
    # session's current database, not the policy's (verified on 24.12).
    for entries in _policies().values():
        for name, using, _to in entries:
            for ref in re.findall(r"\bFROM\s+([\w.]+)", using):
                assert ref.startswith(f"{DB}."), f"{name}: FROM {ref} is not qualified"


@pytest.mark.parametrize(
    ("table", "parent"),
    [
        ("sample", f"FROM {DB}.patient"),
        ("clinical_event", f"FROM {DB}.patient"),
        ("clinical_sample", f"FROM {DB}.sample"),
        ("clinical_event_data", f"FROM {DB}.clinical_event"),
        ("mutation", f"FROM {DB}.genetic_profile"),
        ("structural_variant", f"FROM {DB}.genetic_profile"),
        ("sample_cna_event", f"FROM {DB}.genetic_profile"),
        ("genetic_alteration", f"FROM {DB}.genetic_profile"),
        ("mutation_event", f"FROM {DB}.mutation"),
        ("cna_event", f"FROM {DB}.sample_cna_event"),
        ("generic_assay_data_derived", f"FROM {DB}.genetic_profile"),
        ("patient", f"FROM {DB}.cancer_study"),
        ("genetic_profile", f"FROM {DB}.cancer_study"),
    ],
)
def test_join_through_tables_follow_their_real_parent(table, parent):
    ((_name, using, _to),) = _policies()[table]
    assert parent in using
    # ...and every path ends at the study identifier.
    root = f"FROM {DB}.cancer_study WHERE has(splitByChar(',', {SETTING}), cancer_study_identifier)"
    assert root in using


def test_every_repo_sql_view_is_classified():
    views = _repo_sql_views()
    assert views, "expected CREATE VIEW statements in sql/*.sql"
    assert views == set(STUDY_SCOPED_VIEWS), (
        f"missing from STUDY_SCOPED_VIEWS: {sorted(views - STUDY_SCOPED_VIEWS)}; "
        f"stale: {sorted(STUDY_SCOPED_VIEWS - views)}"
    )


def test_every_repo_sql_table_is_classified():
    created: set[str] = set()
    dropped: set[str] = set()
    for path in sorted((REPO / "sql").glob("*.sql")):
        sql = _strip_comments(path.read_text(encoding="utf-8"))
        created.update(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", sql))
        dropped.update(re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)\s*;\s*$", sql, re.M))
    # Tables a file creates and drops again (sql/6's swap table) never persist.
    transient = {table for table in created & dropped if table not in classified_names()}
    unclassified = created - transient - classified_names()
    assert not unclassified, sorted(unclassified)


def test_every_fixture_table_is_classified():
    tables = _fixture_tables()
    assert tables
    assert set(tables) <= classified_names(), sorted(set(tables) - classified_names())


def test_fixture_sample_and_patient_use_production_linkage():
    tables = _fixture_tables()
    for table in ("sample", "patient", "mutation", "clinical_event", "clinical_event_data"):
        assert (
            "cancer_study_identifier" not in tables[table]
        ), f"{table} has a study-identifier shortcut column the production schema does not have"
    assert re.search(r"\bpatient_id\b", tables["sample"])
    assert re.search(r"\bcancer_study_id\b", tables["patient"])


def test_guard_markers_cover_catalog():
    markers = set(PROTECTED_QUERY_MARKERS)
    assert set(STUDY_SCOPED_TABLES) <= markers
    assert STUDY_SCOPED_VIEWS <= markers
    assert DENIED_TABLES <= markers
    for table in ("structural_variant", "sample_cna_event", "cna_event", "clinical_event_derived"):
        assert table in markers


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM structural_variant",
        "SELECT * FROM sample_cna_event",
        "SELECT * FROM clinical_event_derived",
        "SELECT * FROM gene_mutation_frequency_in_study(study='x', gene='TP53')",
        "SELECT * FROM mutation_panel_gene_coverage",
    ],
)
def test_guard_treats_downstream_tables_and_views_as_protected(query):
    assert references_protected_study_data(query)


def test_every_cataloged_view_is_set_to_sql_security_invoker():
    sql = _strip_comments(POLICY_SQL.read_text(encoding="utf-8"))
    altered = re.findall(rf"ALTER TABLE {re.escape(DB)}\.(\w+) MODIFY SQL SECURITY (\w+);", sql)
    assert sorted(altered) == sorted((v, "INVOKER") for v in STUDY_SCOPED_VIEWS)
    # The ALTERs need the views, so they come after the policies (applied
    # after sql/4, sql/5, sql/7) and nothing else is altered.
    assert len(re.findall(r"\bALTER\b", sql)) == len(STUDY_SCOPED_VIEWS)
