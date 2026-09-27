"""Live parity check for gene_alteration_frequency_by_cancer_type.

The view resolves the gene symbol with an IN-subquery on `gene` instead of
`JOIN gene ... WHERE g.hugo_gene_symbol = ...`. This test builds the legacy
JOIN form from the shipped SQL and asserts both return identical rows on a
fixture covering the edge cases (multi-entrez symbols, duplicate gene rows,
unknown and mixed-case symbols, NULL keys under both transform_null_in
settings). Runs under `clickhouse local` when CLICKHOUSE_BINARY is set.
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
NEW_FILTER = "\n        ".join(
    [
        "WHERE gpl.gene_id IN (",
        "SELECT entrez_gene_id FROM gene",
        "WHERE hugo_gene_symbol = {gene:String} AND entrez_gene_id IS NOT NULL)",
    ]
)
OLD_FILTER = "\n    ".join(
    [
        "JOIN gene g ON gpl.gene_id = g.entrez_gene_id",
        "WHERE g.hugo_gene_symbol = {gene:String}",
    ]
)

# NOPE is not in `gene`; tp53 only differs from TP53 by case. Both have
# genomic events, so their final rows exercise the (WES-only) denominator.
UNMATCHED = ["NOPE", "tp53"]
GENES = ["TP53", "ORD", "MULTI", "DUP", *UNMATCHED]
ALTERATIONS = ["mutation", "amplification", "deep_deletion", "structural_variant"]

FIXTURE = """
CREATE TABLE cancer_study_query_preferences
    (preference_name String, cancer_study_identifier String) ENGINE = Memory;
INSERT INTO cancer_study_query_preferences VALUES ('p', 's1');

CREATE TABLE gene (entrez_gene_id {KEY}, hugo_gene_symbol String) ENGINE = Memory;
INSERT INTO gene VALUES (7157, 'TP53'), (3000, 'ORD'), (1001, 'MULTI'), (1002, 'MULTI'),
    (2000, 'DUP'), (2000, 'DUP'), (4000, 'OTHER');

CREATE TABLE gene_panel (internal_id Int64, stable_id String) ENGINE = Memory;
INSERT INTO gene_panel VALUES (1, 'P1'), (2, 'P2'), (3, 'P3');

CREATE TABLE gene_panel_list (internal_id Int64, gene_id {KEY}) ENGINE = Memory;
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
ARRAY JOIN ['TP53', 'ORD', 'MULTI', 'DUP', 'NOPE', 'tp53'] AS g
ARRAY JOIN [('mutation', NULL), ('cna', 2), ('cna', -2), ('structural_variant', NULL)]
    :: Array(Tuple(String, Nullable(Int8))) AS ev
WHERE cityHash64(number, g, ev.1, ifNull(ev.2, 0)) % 3 = 0;
"""

NULL_KEY_ROWS = """
INSERT INTO gene VALUES (NULL, 'ORD');
INSERT INTO gene_panel_list VALUES (2, NULL);
"""


def _view_body() -> str:
    match = re.search(rf"^CREATE VIEW {VIEW} AS\n(.*?);\n", SQL, flags=re.MULTILINE | re.DOTALL)
    assert match, f"{VIEW} not found in sql/4"
    return match.group(1)


def _panel_branch(body: str) -> str:
    """The gene-specific (non-WES) half of profiled_samples_for_gene."""
    match = re.search(r"profiled_samples_for_gene AS \(\n(.*?)\n    UNION ALL", body, re.DOTALL)
    assert match, "profiled_samples_for_gene panel branch not found"
    return match.group(1)


def test_view_uses_in_subquery_not_gene_join():
    body = _view_body()
    assert NEW_FILTER in body
    assert "JOIN gene g" not in body


@pytest.mark.skipif(not CLICKHOUSE_BINARY, reason="CLICKHOUSE_BINARY not set")
@pytest.mark.parametrize("transform_null_in", [0, 1])
@pytest.mark.parametrize("nullable_keys", [False, True])
def test_in_subquery_matches_legacy_join(nullable_keys, transform_null_in):
    new_body = _view_body()
    old_body = new_body.replace(NEW_FILTER, OLD_FILTER)
    assert old_body != new_body

    selects = []
    for gene in GENES:
        for alteration in ALTERATIONS:
            for variant in ("old", "new"):
                params = f"gene='{gene}', alteration='{alteration}'"
                selects.append(
                    f"SELECT '{variant}|{gene}|{alteration}', * FROM {variant}_view("
                    f"preference='p', {params}) ORDER BY cancer_type FORMAT TSV;"
                )
                selects.append(
                    f"SELECT '{variant}_panel|{gene}|{alteration}', uniqExact(sample_unique_id) "
                    f"FROM {variant}_panel({params}) FORMAT TSV;"
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
            f"SET transform_null_in = {transform_null_in};",
            FIXTURE.replace("{KEY}", "Nullable(Int64)" if nullable_keys else "Int64"),
            NULL_KEY_ROWS if nullable_keys else "",
            f"CREATE VIEW old_view AS\n{old_body};",
            f"CREATE VIEW new_view AS\n{new_body};",
            f"CREATE VIEW old_panel AS\n{_panel_branch(old_body)};",
            f"CREATE VIEW new_panel AS\n{_panel_branch(new_body)};",
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
            key = f"{gene}|{alteration}"
            old, new = results.get(f"old|{key}", []), results.get(f"new|{key}", [])
            old_panel, new_panel = results[f"old_panel|{key}"], results[f"new_panel|{key}"]
            assert old == new, key
            assert old_panel == new_panel, key
            assert len(new) == 2, (key, new)
            if gene in UNMATCHED:
                # No panel sample is profiled; the denominator is WES only.
                assert new_panel == ["0"], key
                assert [row.split("\t")[2] for row in new] == ["50", "50"], (key, new)
            else:
                assert new_panel != ["0"], key
