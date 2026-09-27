"""Live parity check for gene_alteration_frequency_by_cancer_type.

The view resolves the gene symbol with an IN-subquery on `gene` instead of
`JOIN gene ... WHERE g.hugo_gene_symbol = ...`. This test builds the legacy
JOIN form from the shipped SQL and asserts both return identical rows on a
fixture covering the edge cases (multi-entrez symbols, duplicate gene rows,
unknown symbols). Runs under `clickhouse local` when CLICKHOUSE_BINARY is set.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SQL = (ROOT / "sql" / "4-mutation-frequency-views.sql").read_text()
CLICKHOUSE_BINARY = os.environ.get("CLICKHOUSE_BINARY")

VIEW = "gene_alteration_frequency_by_cancer_type"
NEW_FILTER = (
    "WHERE gpl.gene_id IN (SELECT entrez_gene_id FROM gene WHERE hugo_gene_symbol = {gene:String})"
)
OLD_FILTER = "\n    ".join(
    [
        "JOIN gene g ON gpl.gene_id = g.entrez_gene_id",
        "WHERE g.hugo_gene_symbol = {gene:String}",
    ]
)

GENES = ["TP53", "ORD", "MULTI", "DUP", "NOPE"]
ALTERATIONS = ["mutation", "amplification", "deep_deletion", "structural_variant"]

# 480 samples: 400 in cohort study s1 (200 per cancer type, 50 per panel
# per type), 80 in s2 outside the cohort. Panel assignment by i % 4.
#   P1: TP53, ORD, MULTI(1001), DUP     P2: TP53, MULTI(1002)
#   P3: TP53, MULTI(1001+1002), DUP     WES
# MULTI maps to two entrez ids; DUP has two identical gene rows.
FIXTURE = """
CREATE TABLE cancer_study_query_preferences
    (preference_name String, cancer_study_identifier String) ENGINE = Memory;
INSERT INTO cancer_study_query_preferences VALUES ('p', 's1');

CREATE TABLE gene (entrez_gene_id Int64, hugo_gene_symbol String) ENGINE = Memory;
INSERT INTO gene VALUES (7157, 'TP53'), (3000, 'ORD'), (1001, 'MULTI'), (1002, 'MULTI'),
    (2000, 'DUP'), (2000, 'DUP'), (4000, 'OTHER');

CREATE TABLE gene_panel (internal_id Int64, stable_id String) ENGINE = Memory;
INSERT INTO gene_panel VALUES (1, 'P1'), (2, 'P2'), (3, 'P3');

CREATE TABLE gene_panel_list (internal_id Int64, gene_id Int64) ENGINE = Memory;
INSERT INTO gene_panel_list VALUES (1, 7157), (1, 3000), (1, 1001), (1, 2000),
    (2, 7157), (2, 1002), (2, 4000), (3, 7157), (3, 1001), (3, 1002), (3, 2000);

CREATE TABLE sample_to_gene_panel_derived (
    sample_unique_id String, alteration_type String, gene_panel_id String,
    cancer_study_identifier String) ENGINE = Memory;
INSERT INTO sample_to_gene_panel_derived
SELECT concat('S', toString(number)), at,
       ['P1', 'P2', 'P3', 'WES'][number % 4 + 1],
       if(number < 400, 's1', 's2')
FROM numbers(480)
ARRAY JOIN ['MUTATION_EXTENDED', 'COPY_NUMBER_ALTERATION', 'STRUCTURAL_VARIANT'] AS at;

CREATE TABLE clinical_data_derived (
    sample_unique_id String, attribute_name String, attribute_value String,
    cancer_study_identifier String) ENGINE = Memory;
INSERT INTO clinical_data_derived
SELECT concat('S', toString(number)), 'CANCER_TYPE',
       if(intDiv(number, 4) % 2 = 0, 'CT_A', 'CT_B'), if(number < 400, 's1', 's2')
FROM numbers(480);

CREATE TABLE genomic_event_derived (
    sample_unique_id String, hugo_gene_symbol String, variant_type String,
    mutation_status String, cna_alteration Nullable(Int8), off_panel UInt8,
    cancer_study_identifier String) ENGINE = Memory;
INSERT INTO genomic_event_derived
SELECT concat('S', toString(number)), g, ev.1, 'SOMATIC', ev.2, 0,
       if(number < 400, 's1', 's2')
FROM numbers(480)
ARRAY JOIN ['TP53', 'ORD', 'MULTI', 'DUP'] AS g
ARRAY JOIN [('mutation', NULL), ('cna', 2), ('cna', -2), ('structural_variant', NULL)]
    :: Array(Tuple(String, Nullable(Int8))) AS ev
WHERE cityHash64(number, g, ev.1, ifNull(ev.2, 0)) % 3 = 0;
"""


def _view_body() -> str:
    match = re.search(rf"^CREATE VIEW {VIEW} AS\n(.*?);\n", SQL, flags=re.MULTILINE | re.DOTALL)
    assert match, f"{VIEW} not found in sql/4"
    return match.group(1)


def test_view_uses_in_subquery_not_gene_join():
    body = _view_body()
    assert NEW_FILTER in body
    assert "JOIN gene g" not in body


@pytest.mark.skipif(not CLICKHOUSE_BINARY, reason="CLICKHOUSE_BINARY not set")
def test_in_subquery_matches_legacy_join():
    new_body = _view_body()
    old_body = new_body.replace(NEW_FILTER, OLD_FILTER)
    assert old_body != new_body

    selects = []
    for gene in GENES:
        for alteration in ALTERATIONS:
            for variant in ("old", "new"):
                selects.append(
                    f"SELECT '{variant}|{gene}|{alteration}', * FROM {variant}_view("
                    f"preference='p', gene='{gene}', alteration='{alteration}') "
                    "ORDER BY cancer_type FORMAT TSV;"
                )
    # Prove the edge cases are exercised: the legacy JOIN fans out rows for
    # MULTI (sample on P3 matches two entrez ids) and DUP (duplicate gene row).
    selects.append(
        "SELECT 'fanout|' || g.hugo_gene_symbol, count() FROM gene_panel_list gpl "
        "JOIN gene g ON gpl.gene_id = g.entrez_gene_id WHERE gpl.internal_id = 3 "
        "GROUP BY g.hugo_gene_symbol ORDER BY 1 FORMAT TSV;"
    )
    script = "\n".join(
        [
            FIXTURE,
            f"CREATE VIEW old_view AS\n{old_body};",
            f"CREATE VIEW new_view AS\n{new_body};",
            *selects,
        ]
    )
    proc = subprocess.run(
        [CLICKHOUSE_BINARY, "local", "--multiquery"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    results: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        label, _, rest = line.partition("\t")
        results.setdefault(label, []).append(rest)

    assert results.get("fanout|MULTI") == ["2"]
    assert results.get("fanout|DUP") == ["2"]

    for gene in GENES:
        for alteration in ALTERATIONS:
            old = results.get(f"old|{gene}|{alteration}", [])
            new = results.get(f"new|{gene}|{alteration}", [])
            assert old == new, (gene, alteration)
            if gene == "NOPE":
                assert new == []
            else:
                assert len(new) == 2, (gene, alteration, new)
