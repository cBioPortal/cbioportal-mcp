"""DDL contract plus opt-in ClickHouse 24.8 execution against upstream tables."""

import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SQL = (ROOT / "sql/10-dictionaries.sql").read_text()
SCHEMA = (ROOT / "tests/fixtures/dictionary_upstream_schema.sql").read_text()
DDL = re.sub(r"--[^\n]*", "", SQL)
DICTIONARIES = re.findall(
    r"CREATE OR REPLACE DICTIONARY (\w+)\s*\((.*?)\)\s*"
    r"PRIMARY KEY (\w+)\s*SOURCE\(CLICKHOUSE\(TABLE '(\w+)'\)\)"
    r"\s*LAYOUT\(COMPLEX_KEY_HASHED\(\)\)\s*LIFETIME\(0\);",
    DDL,
    re.S,
)
TABLES = {
    name: set(re.findall(r"`(\w+)`", columns))
    for name, columns in re.findall(r"CREATE TABLE (\w+) \((.*?)\) ENGINE", SCHEMA, re.S)
}
VIEWS = dict(re.findall(r"CREATE OR REPLACE VIEW (\w+) AS\s*(.*?);", DDL, re.S))


def test_all_objects_are_replaceable_and_daily_snapshots():
    assert len(DICTIONARIES) == 4
    assert len(VIEWS) == 2
    assert len(re.findall(r"\bCREATE\b", DDL)) == 6
    assert not re.search(r"\b(DROP|ALTER|INSERT|DELETE)\b", DDL)
    assert {name for name, *_ in DICTIONARIES} == {
        "gene_by_entrez_dict",
        "gene_by_symbol_dict",
        "study_by_identifier_dict",
        "genetic_profile_by_stable_id_dict",
    }


@pytest.mark.parametrize("name,columns,key,source", DICTIONARIES)
def test_dictionary_attributes_exist_in_upstream_source(name, columns, key, source):
    attributes = set(re.findall(r"^\s*(\w+)\s+(?:String|Int64|UInt64|Array)", columns, re.M))
    assert key in attributes, name
    assert source in TABLES or source in VIEWS, source
    available = TABLES.get(source)
    if available is None:
        available = set(re.findall(r"\bAS (\w+)", VIEWS[source]))
    assert attributes <= available, (name, attributes - available)


def test_source_views_reference_real_upstream_tables_and_columns():
    for query in VIEWS.values():
        aliases = dict(
            (alias, table) for table, alias in re.findall(r"(?:FROM|JOIN) (\w+) AS (\w+)", query)
        )
        assert aliases
        for table in aliases.values():
            assert table in TABLES, table
        for alias, column in re.findall(r"\b(\w+)\.(\w+)\b", query):
            if alias == "counts":
                assert re.search(rf"\bAS {column}\b", query)
            else:
                assert alias in aliases, alias
                assert column in TABLES[aliases[alias]], (alias, column)


def test_sources_are_local_and_do_not_depend_on_script_six():
    assert not re.search(r"\b(HOST|PORT|USER|PASSWORD|DB)\b", DDL)
    assert "cs.sample_count" not in DDL
    assert "WHERE cs.cancer_study_identifier IS NOT NULL" in DDL
    assert "arraySort(groupUniqArray(g.entrez_gene_id))" in DDL


def test_clickhouse_dictionary_results_and_refresh():
    """Run with CH_DICTIONARY_TEST_CONTAINER=ch-dict-test; no published ports needed."""
    container = os.environ.get("CH_DICTIONARY_TEST_CONTAINER")
    if not container:
        pytest.skip("set CH_DICTIONARY_TEST_CONTAINER to run Docker integration")
    database = "dict_test_" + uuid.uuid4().hex

    def query(sql, db=database):
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "clickhouse-client",
                "--database",
                db,
                "--multiquery",
                "--format",
                "TSV",
            ],
            input=sql,
            text=True,
            capture_output=True,
            timeout=60,
            check=True,
        )
        return result.stdout.strip()

    query(f"CREATE DATABASE {database}", "default")
    try:
        query(SCHEMA)
        query(
            """
            INSERT INTO gene VALUES (7157, 'TP53', 1, NULL),
                (1, 'SHARED', 2, NULL), (-2, 'SHARED', 3, NULL);
            INSERT INTO cancer_study (cancer_study_id, cancer_study_identifier,
                name, type_of_cancer_id) VALUES
                (1, 'study_a', 'Study A', 'brca'), (2, 'empty', 'Empty', 'luad'),
                (3, NULL, 'Null ID', 'brca');
            INSERT INTO genetic_profile (genetic_profile_id, stable_id, cancer_study_id,
                genetic_alteration_type) VALUES (10, 'study_a_mutations', 1, 'MUTATION_EXTENDED');
            INSERT INTO sample_list (list_id, stable_id, cancer_study_id) VALUES
                (1, 'study_a_all', 1), (2, 'study_a_sequenced', 1), (3, 'study_a_cna', 1),
                (4, 'study_a_unrelated', 1);
            INSERT INTO sample_list_list VALUES (1, 11), (1, 12), (2, 11), (3, 12), (4, 13);
        """
        )
        query(SQL)
        # Load every dictionary and compare complete tuples to direct source queries.
        comparisons = [
            (
                "SELECT entrez_gene_id, hugo_gene_symbol FROM gene ORDER BY entrez_gene_id",
                "SELECT entrez_gene_id, dictGet('gene_by_entrez_dict', 'hugo_gene_symbol', "
                "tuple(entrez_gene_id)) FROM gene ORDER BY entrez_gene_id",
            ),
            (
                "SELECT hugo_gene_symbol, arraySort(groupUniqArray(entrez_gene_id)) "
                "FROM gene GROUP BY hugo_gene_symbol ORDER BY hugo_gene_symbol",
                "SELECT DISTINCT hugo_gene_symbol, dictGet('gene_by_symbol_dict', "
                "'entrez_gene_ids', tuple(hugo_gene_symbol)) FROM gene ORDER BY hugo_gene_symbol",
            ),
            (
                "SELECT stable_id, tuple(genetic_profile_id, cancer_study_id, "
                "genetic_alteration_type) FROM genetic_profile ORDER BY stable_id",
                "SELECT stable_id, dictGet('genetic_profile_by_stable_id_dict', "
                "('genetic_profile_id', 'cancer_study_id', 'genetic_alteration_type'), "
                "tuple(stable_id)) FROM genetic_profile ORDER BY stable_id",
            ),
            (
                "SELECT cancer_study_identifier, tuple(cancer_study_id, name, type_of_cancer_id, "
                "sample_count, mutation_sample_count, cna_sample_count) "
                "FROM dictionary_study_source ORDER BY cancer_study_identifier",
                "SELECT cancer_study_identifier, dictGet('study_by_identifier_dict', "
                "('cancer_study_id', 'name', 'type_of_cancer_id', 'sample_count', "
                "'mutation_sample_count', 'cna_sample_count'), tuple(cancer_study_identifier)) "
                "FROM dictionary_study_source ORDER BY cancer_study_identifier",
            ),
        ]
        for direct, lookup in comparisons:
            assert query(direct) == query(lookup)
        assert (
            query(
                "SELECT sample_count, mutation_sample_count, cna_sample_count "
                "FROM dictionary_study_source ORDER BY cancer_study_identifier"
            )
            == "0\t0\t0\n2\t1\t1"
        )
        assert query("SELECT count() FROM dictionary_study_source") == "2"
        for name, _, key, _ in DICTIONARIES:
            missing = "toInt64(99999)" if key == "entrez_gene_id" else "'missing'"
            assert query(f"SELECT dictHas('{name}', tuple({missing}))") == "0"
        # Source DB must remain bound when the caller uses a different database.
        assert (
            query(
                f"SELECT dictGet('{database}.gene_by_entrez_dict', "
                "'hugo_gene_symbol', tuple(toInt64(-2)))",
                "default",
            )
            == "SHARED"
        )
        query("INSERT INTO gene VALUES (99, 'NEW', 99, NULL)")
        assert query("SELECT dictHas('gene_by_entrez_dict', tuple(toInt64(99)))") == "0"
        query(SQL)  # Idempotent, also invalidates already-loaded LIFETIME(0) dictionaries.
        assert (
            query("SELECT dictGet('gene_by_entrez_dict', 'hugo_gene_symbol', tuple(toInt64(99)))")
            == "NEW"
        )
        for direct, lookup in comparisons:
            assert query(direct) == query(lookup)
    finally:
        query(f"DROP DATABASE IF EXISTS {database} SYNC", "default")
