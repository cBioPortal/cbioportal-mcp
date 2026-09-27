"""Static sanity checks for sql/9-projections.sql.

The file runs unattended in the daily clone CronJob after every derived-table
rebuild, so it must be idempotent and must only reference tables/columns that
exist in the upstream cBioPortal ClickHouse DDL.
"""

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
PROJECTIONS_SQL = SQL_DIR / "9-projections.sql"

# Columns of the derived tables as created by cBioPortal's
# src/main/resources/db-scripts/clickhouse/clickhouse.sql (derived schema
# version 1.0.11). Update if upstream adds/renames columns used here.
UPSTREAM_DERIVED_COLUMNS = {
    "genomic_event_derived": {
        "sample_unique_id",
        "hugo_gene_symbol",
        "entrez_gene_id",
        "gene_panel_stable_id",
        "cancer_study_identifier",
        "genetic_profile_stable_id",
        "variant_type",
        "mutation_variant",
        "mutation_type",
        "mutation_status",
        "driver_filter",
        "driver_filter_annotation",
        "driver_tiers_filter",
        "driver_tiers_filter_annotation",
        "cna_alteration",
        "cna_cytoband",
        "sv_event_info",
        "patient_unique_id",
        "off_panel",
    },
    "sample_to_gene_panel_derived": {
        "sample_unique_id",
        "alteration_type",
        "gene_panel_id",
        "cancer_study_identifier",
        "genetic_profile_id",
    },
}

ADD_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+PROJECTION\s+(?P<ine>IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>\w+)\s*\((?P<body>.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
MATERIALIZE_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+MATERIALIZE\s+PROJECTION\s+(?P<name>\w+)"
    r"(?P<rest>[^;]*);",
    re.IGNORECASE,
)


def _sql() -> str:
    text = PROJECTIONS_SQL.read_text()
    return re.sub(r"--[^\n]*", "", text)


def _statements() -> list[str]:
    return [s.strip() for s in _sql().split(";") if s.strip()]


def _projections() -> list[re.Match]:
    return list(ADD_RE.finditer(_sql()))


def _split_top_level(expr: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _select_and_order_columns(body: str) -> tuple[list[str], list[str]]:
    m = re.match(r"\s*SELECT\s+(?P<sel>.*?)\s+ORDER\s+BY\s+(?P<ord>.*)$", body, re.I | re.S)
    assert m, f"projection body must be 'SELECT ... ORDER BY ...': {body!r}"
    order = m.group("ord").strip()
    if order.startswith("(") and order.endswith(")"):
        order = order[1:-1]
    return _split_top_level(m.group("sel")), _split_top_level(order)


def test_file_is_numbered_and_listed_in_readme():
    assert re.match(r"^\d+-", PROJECTIONS_SQL.name)
    numbered = sorted(p.name for p in SQL_DIR.glob("*.sql"))
    # Must run after the recipe views it accelerates.
    assert numbered.index(PROJECTIONS_SQL.name) > numbered.index("4-mutation-frequency-views.sql")
    assert f"`{PROJECTIONS_SQL.name}`" in (SQL_DIR / "README.md").read_text()


def test_only_projection_ddl():
    """No DROP/CREATE/INSERT/DELETE: the file may only add + materialize projections."""
    for stmt in _statements():
        assert re.match(
            r"ALTER\s+TABLE\s+\w+\s+(ADD|MATERIALIZE)\s+PROJECTION\b", stmt, re.I
        ), f"unexpected statement: {stmt[:80]!r}"


def test_add_projection_is_idempotent():
    adds = _projections()
    assert adds, "no ADD PROJECTION statements found"
    n_add_statements = len(re.findall(r"\bADD\s+PROJECTION\b", _sql(), re.I))
    assert len(adds) == n_add_statements, "an ADD PROJECTION statement failed to parse"
    for m in adds:
        assert m.group("ine"), f"{m.group('name')}: ADD PROJECTION must use IF NOT EXISTS"


def test_projection_names_are_unique():
    names = [m.group("name") for m in _projections()]
    assert len(names) == len(set(names))


def test_every_projection_is_materialized_synchronously():
    added = {(m.group("table"), m.group("name")) for m in _projections()}
    materialized = {}
    for m in MATERIALIZE_RE.finditer(_sql()):
        materialized[(m.group("table"), m.group("name"))] = m.group("rest")
    assert added == set(materialized), "each ADD PROJECTION needs a matching MATERIALIZE"
    for key, rest in materialized.items():
        assert re.search(r"mutations_sync\s*=\s*2", rest), f"{key}: needs mutations_sync = 2"
    # MATERIALIZE must follow its ADD.
    sql = _sql()
    for _table, name in added:
        add_pos = re.search(rf"ADD\s+PROJECTION\s+IF\s+NOT\s+EXISTS\s+{name}\b", sql).start()
        mat_pos = re.search(rf"MATERIALIZE\s+PROJECTION\s+{name}\b", sql).start()
        assert add_pos < mat_pos, f"{name}: MATERIALIZE before ADD"


def test_projections_target_existing_tables_and_columns():
    other_sql = "\n".join(p.read_text() for p in SQL_DIR.glob("*.sql") if p != PROJECTIONS_SQL)
    for m in _projections():
        table = m.group("table")
        assert table in UPSTREAM_DERIVED_COLUMNS, f"unknown table {table}"
        # The repo's own recipes already depend on the table existing.
        assert re.search(rf"\b{table}\b", other_sql), f"{table} not used by any other sql/ file"
        columns = UPSTREAM_DERIVED_COLUMNS[table]
        select_cols, order_cols = _select_and_order_columns(m.group("body"))
        if select_cols != ["*"]:
            unknown = set(select_cols) - columns
            assert not unknown, f"{m.group('name')}: unknown columns {unknown}"
            missing_from_select = set(order_cols) - set(select_cols)
            assert (
                not missing_from_select
            ), f"{m.group('name')}: ORDER BY columns not selected {missing_from_select}"
        unknown_order = set(order_cols) - columns
        assert not unknown_order, f"{m.group('name')}: unknown ORDER BY columns {unknown_order}"


def test_expected_sort_orders():
    """Pin the key shapes the benchmarks in sql/README.md were measured with."""
    orders = {
        m.group("name"): _select_and_order_columns(m.group("body"))[1] for m in _projections()
    }
    assert orders["ged_by_study_gene"][:3] == [
        "cancer_study_identifier",
        "hugo_gene_symbol",
        "variant_type",
    ]
    assert orders["ged_by_gene_study"][:3] == [
        "hugo_gene_symbol",
        "variant_type",
        "cancer_study_identifier",
    ]
    assert orders["stgp_by_study"][0] == "cancer_study_identifier"
