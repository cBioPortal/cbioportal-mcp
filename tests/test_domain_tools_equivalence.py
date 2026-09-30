"""Precomputed aggregates == live recipes, on a real ClickHouse.

Opt-in: set CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL to a server you can create
scratch databases on. Docker or a native binary both work:

    docker run -d --name ch -p 28123:8123 -p 29000:9000 -e CLICKHOUSE_USER=admin \\
        -e CLICKHOUSE_PASSWORD=admin clickhouse/clickhouse-server:24.8
    CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL=http://admin:admin@localhost:28123 \\
    CBIOPORTAL_MCP_TEST_CLICKHOUSE_NATIVE_PORT=29000 \\
        uv run pytest tests/test_domain_tools_equivalence.py

The module loads the synthetic fixture (tests/aggregate_fixture.py) into a
fresh database and applies the SQL in the real phase order: sql/3 and sql/4,
then the fixture's preference rows (standing in for sql/portal-specific/),
then sql/final/. It then checks three independent implementations against
each other: the sql/final tables, the canonical sql/4 recipe views (and the
tools' live-fallback SQL), and a pure-Python oracle over the generated rows.

test_real_apply_order_* additionally runs scripts/apply_sql.sh itself against
the real sql/3, sql/4, sql/portal-specific/public-portal and sql/final files.
It needs CBIOPORTAL_MCP_TEST_CLICKHOUSE_NATIVE_PORT and `clickhouse-client` on
PATH (a symlink named clickhouse-client to the `clickhouse` binary works).
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aggregate_fixture as fx
import pytest

from cbioportal_mcp import domain_tools, server
from cbioportal_mcp.result_format import table_to_records as _records

URL = os.getenv("CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL")
NATIVE_PORT = os.getenv("CBIOPORTAL_MCP_TEST_CLICKHOUSE_NATIVE_PORT")
pytestmark = pytest.mark.skipif(not URL, reason="CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL not set")

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "sql"
AGGREGATES_SQL = SQL_DIR / "final" / "0-precomputed-aggregates.sql"
ALTERATIONS = ["mutation", "amplification", "deep_deletion", "structural_variant"]
AGGREGATE_TABLES = (
    "study_gene_alteration_counts",
    "cancer_type_gene_alteration_counts",
    "study_profiled_counts",
)


def _statements(path: Path) -> list[str]:
    """Split a sql file the way clickhouse-client --queries-file would."""
    body = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in body.split(";") if s.strip()]


def _client(**kwargs):
    import clickhouse_connect

    # No session: with one, a query that outlives the HTTP timeout keeps the
    # session locked and every later query fails with SESSION_IS_LOCKED.
    return clickhouse_connect.get_client(
        dsn=URL, autogenerate_session_id=False, send_receive_timeout=900, **kwargs
    )


def _apply(client, path: Path):
    for stmt in _statements(path):
        client.command(stmt)


def _load(client, tables):
    for ddl in fx.DDL:
        client.command(ddl)
    for name, rows in tables.items():
        columns = [c.strip() for c in fx.INSERT_COLUMNS[name].split(",")]
        client.insert(name, rows, column_names=columns)


@pytest.fixture(scope="module")
def ch():
    admin = _client()
    db = f"mcp_aggtest_{uuid.uuid4().hex[:10]}"
    admin.command(f"CREATE DATABASE {db}")
    client = _client(database=db)
    try:
        tables = fx.generate()
        preferences = tables.pop("cancer_study_query_preferences")
        _load(client, tables)
        # Phase 1 (portable): sql/3 creates the preferences table, sql/4 the views.
        _apply(client, SQL_DIR / "3-add-cancer-study-query-preferences.sql")
        _apply(client, SQL_DIR / "4-mutation-frequency-views.sql")
        # Phase 2 (portal-specific): the fixture cohorts.
        client.insert(
            "cancer_study_query_preferences",
            preferences,
            column_names=["preference_name", "cancer_study_identifier", "notes"],
        )
        # Phase 3 (final): the aggregates.
        _apply(client, AGGREGATES_SQL)
        tables["cancer_study_query_preferences"] = preferences
        yield client, tables
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {db}")


def _rows(client, sql):
    res = client.query(sql)
    return [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]


def _union(parts: list[str]) -> str:
    return " UNION ALL ".join(f"SELECT * FROM ({p})" for p in parts)


def _point_tools_at(client, monkeypatch):
    labels = []

    def run_select_query(query, *, query_label, max_rows=None):
        labels.append(query_label)
        res = client.query(query)
        return server.zip_select_query_result(
            {"columns": res.column_names, "rows": res.result_rows}
        )

    monkeypatch.setattr(server, "run_select_query", run_select_query)
    return labels


@pytest.fixture
def tool_db(ch, monkeypatch):
    """Point the tools' run_select_query at the scratch database."""
    client, _ = ch
    return _point_tools_at(client, monkeypatch)


# --- sql/final tables vs the pure-Python oracle ------------------------------


def test_study_table_matches_oracle(ch):
    client, tables = ch
    got = {
        (r["cancer_study_identifier"], r["hugo_gene_symbol"], r["alteration_type"]): (
            r["altered_samples"],
            r["profiled_samples"],
            r["altered_events"],
        )
        for r in _rows(client, "SELECT * FROM study_gene_alteration_counts")
    }
    assert got == fx.oracle_study_counts(tables)


def test_cancer_type_table_matches_oracle(ch):
    client, tables = ch
    got = {
        (r["preference_name"], r["cancer_type"], r["hugo_gene_symbol"], r["alteration_type"]): (
            r["altered_samples"],
            r["profiled_samples"],
        )
        for r in _rows(client, "SELECT * FROM cancer_type_gene_alteration_counts")
    }
    assert got == fx.oracle_cancer_type_counts(tables)


def test_profiled_counts_table_matches_oracle(ch):
    client, tables = ch
    got = {
        (r["cancer_study_identifier"], r["profile_type"]): (
            r["samples"],
            r["patients"],
            r["wes_samples"],
        )
        for r in _rows(client, "SELECT * FROM study_profiled_counts")
    }
    assert got == fx.oracle_profiled_counts(tables)


def test_fixture_exercises_the_edge_cases(ch):
    """Guard against a fixture change silently removing what the tests rely on."""
    client, _ = ch

    def n(sql):
        return _rows(client, sql)[0]["n"]

    assert (
        n(
            """
        SELECT count() AS n FROM (
            SELECT sample_unique_id FROM sample_to_gene_panel_derived
            WHERE alteration_type = 'MUTATION_EXTENDED'
            GROUP BY sample_unique_id HAVING has(groupArray(gene_panel_id), 'WES') AND count() > 1)
    """
        )
        > 0
    )
    assert n("SELECT count() AS n FROM genomic_event_derived WHERE off_panel") > 0
    assert (
        n(
            """
        SELECT count() AS n FROM genomic_event_derived
        WHERE variant_type = 'mutation' AND mutation_status = 'UNCALLED'
    """
        )
        > 0
    )
    assert (
        n(
            """
        SELECT count() AS n FROM genomic_event_derived
        WHERE variant_type = 'structural_variant' AND mutation_status = 'UNCALLED'
    """
        )
        > 0
    )
    # Samples profiled for CNA only by a continuous (log2) profile.
    assert (
        n(
            """
        SELECT count() AS n FROM (
            SELECT sample_unique_id FROM sample_to_gene_panel_derived stgp
            JOIN genetic_profile gp ON stgp.genetic_profile_id = gp.stable_id
            WHERE stgp.alteration_type = 'COPY_NUMBER_ALTERATION'
            GROUP BY sample_unique_id HAVING NOT has(groupArray(gp.datatype), 'DISCRETE'))
    """
        )
        > 0
    )
    assert (
        n(
            """
        SELECT count() AS n FROM cancer_type_gene_alteration_counts WHERE profiled_samples >= 50
    """
        )
        > 50
    )


def test_uncalled_svs_change_the_fixture_result(ch):
    """Without the UNCALLED-SV filter the SV numbers would differ.

    Codex's review found pref_all / Breast Cancer / ALK at 36 vs 29 before the
    fix; the exact numbers depend on the fixture, the difference must not be 0.
    """
    client, _ = ch
    rows = _rows(
        client,
        """
        SELECT uniqExact(sample_unique_id) AS all_sv,
               uniqExactIf(sample_unique_id, mutation_status != 'UNCALLED') AS called_sv
        FROM genomic_event_derived
        WHERE variant_type = 'structural_variant' AND off_panel = 0
    """,
    )
    assert rows[0]["all_sv"] > rows[0]["called_sv"]


# --- sql/final tables vs the canonical recipe views (sql/4) ------------------


def _recipe_vs_table(client, recipe_parts, table_sql, key):
    recipe = _rows(client, _union(recipe_parts))
    table = _rows(client, table_sql)
    return sorted(table, key=key), sorted(recipe, key=key)


@pytest.mark.parametrize("preference", sorted(fx.PREFERENCES))
@pytest.mark.parametrize("alteration", ALTERATIONS)
def test_cancer_type_table_equals_gene_alteration_frequency_by_cancer_type(
    ch, preference, alteration
):
    client, _ = ch
    table, recipe = _recipe_vs_table(
        client,
        [
            f"""
            SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
            FROM gene_alteration_frequency_by_cancer_type(
                preference='{preference}', gene='{gene}', alteration='{alteration}')
            """
            for gene in fx.GENES
        ],
        f"""
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = '{preference}' AND alteration_type = '{alteration}'
          AND profiled_samples >= 50
        """,
        key=lambda r: (r["gene"], r["cancer_type"]),
    )
    assert table == recipe


def test_cancer_type_comparison_is_not_vacuous(ch):
    client, _ = ch
    per_alteration = _rows(
        client,
        """
        SELECT alteration_type, count() AS n FROM cancer_type_gene_alteration_counts
        WHERE profiled_samples >= 50 GROUP BY alteration_type
    """,
    )
    assert {r["alteration_type"] for r in per_alteration if r["n"] > 0} == set(ALTERATIONS) | {
        "any"
    }


@pytest.mark.parametrize("preference", sorted(fx.PREFERENCES))
def test_cancer_type_table_equals_gene_mutation_frequency_by_cancer_type(ch, preference):
    client, _ = ch
    table, recipe = _recipe_vs_table(
        client,
        [
            f"""
            SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
            FROM gene_mutation_frequency_by_cancer_type(preference='{preference}', gene='{gene}')
            """
            for gene in fx.GENES
        ],
        f"""
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = '{preference}' AND alteration_type = 'mutation'
          AND profiled_samples >= 50
        """,
        key=lambda r: (r["gene"], r["cancer_type"]),
    )
    assert table == recipe


def test_single_study_preference_equals_gene_mutation_frequency_in_study(ch):
    client, _ = ch
    table, recipe = _recipe_vs_table(
        client,
        [
            f"""
            SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
            FROM gene_mutation_frequency_in_study(study='study_panel', gene='{gene}')
            """
            for gene in fx.GENES
        ],
        """
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = 'pref_panel' AND alteration_type = 'mutation'
          AND profiled_samples >= 50
        """,
        key=lambda r: (r["gene"], r["cancer_type"]),
    )
    assert table == recipe


# study_mixed has samples that are WES for one mutation profile AND on a panel
# for another. With the deduplicated denominators in the top_* views, every
# study must match exactly, overlapping coverage included.
STUDIES_WITH_DATA = ["study_wes", "study_panel", "study_mixed"]


def test_study_mixed_really_has_overlapping_coverage(ch):
    client, _ = ch
    assert (
        _rows(
            client,
            """
        SELECT count() AS n FROM (
            SELECT sample_unique_id FROM mutation_wes_coverage
            WHERE cancer_study_identifier = 'study_mixed'
            INTERSECT
            SELECT sample_unique_id FROM mutation_panel_gene_coverage
            WHERE cancer_study_identifier = 'study_mixed')
    """,
        )[0]["n"]
        > 0
    )


@pytest.mark.parametrize("study", STUDIES_WITH_DATA)
def test_study_table_equals_top_mutated_genes_in_study(ch, study):
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, frequency_pct,
               total_mutation_events
        FROM top_mutated_genes_in_study(study='{study}', top_n=1000)
    """,
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples,
               altered_events AS total_mutation_events
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier = '{study}' AND alteration_type = 'mutation'
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
    """,
    )
    for r in table:
        r["frequency_pct"] = domain_tools._frequency_pct(
            r["altered_samples"], r["profiled_samples"]
        )
    assert recipe and table == [
        {
            k: r[k]
            for k in (
                "hugo_gene_symbol",
                "altered_samples",
                "profiled_samples",
                "total_mutation_events",
                "frequency_pct",
            )
        }
        for r in recipe
    ]


@pytest.mark.parametrize(
    "preference,study", [("pref_mixed", "study_mixed"), ("pref_panel", "study_panel")]
)
def test_single_study_cohort_equals_top_mutated_genes_in_cohort(ch, preference, study):
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, frequency_pct
        FROM top_mutated_genes_in_cohort(preference='{preference}', top_n=1000)
    """,
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier = '{study}' AND alteration_type = 'mutation'
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
    """,
    )
    for r in table:
        r["frequency_pct"] = domain_tools._frequency_pct(
            r["altered_samples"], r["profiled_samples"]
        )
    assert recipe and table == recipe


@pytest.mark.parametrize("study", STUDIES_WITH_DATA)
def test_study_table_equals_top_cna_genes_in_study(ch, study):
    """Includes study_mixed: log2-only samples must not enter the CNA denominator."""
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol,
               if(cna_type = 'AMP', 'amplification', 'deep_deletion') AS alteration_type,
               altered_samples, profiled_samples, frequency_pct
        FROM top_cna_genes_in_study(study='{study}', top_n=1000)
    """,
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, alteration_type, altered_samples, profiled_samples
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier = '{study}'
          AND alteration_type IN ('amplification', 'deep_deletion')
    """,
    )
    for r in table:
        r["frequency_pct"] = domain_tools._frequency_pct(
            r["altered_samples"], r["profiled_samples"]
        )
    key = lambda r: (r["hugo_gene_symbol"], r["alteration_type"])  # noqa: E731
    assert recipe and sorted(table, key=key) == sorted(recipe, key=key)


@pytest.mark.parametrize("study", STUDIES_WITH_DATA)
def test_study_table_equals_top_sv_genes_in_study(ch, study):
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, frequency_pct,
               total_sv_events
        FROM top_sv_genes_in_study(study='{study}', top_n=1000)
    """,
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples,
               altered_events AS total_sv_events
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier = '{study}' AND alteration_type = 'structural_variant'
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
    """,
    )
    for r in table:
        r["frequency_pct"] = domain_tools._frequency_pct(
            r["altered_samples"], r["profiled_samples"]
        )
    key = lambda r: r["hugo_gene_symbol"]  # noqa: E731
    assert recipe and sorted(table, key=key) == sorted(recipe, key=key)


def test_co_altered_denominators_are_the_distinct_union(ch):
    """mutant_profiled + wildtype_profiled = distinct samples profiled for both genes."""
    client, _ = ch
    rows = _rows(
        client,
        """
        SELECT hugo_gene_symbol, mutant_profiled + wildtype_profiled AS profiled
        FROM co_altered_genes_in_study(study='study_mixed', gene='TP53', top_n=1000)
    """,
    )
    assert rows
    for r in rows:
        expected = _rows(
            client,
            f"""
            SELECT uniqExact(sample_unique_id) AS n FROM (
                SELECT sample_unique_id FROM mutation_wes_coverage
                WHERE cancer_study_identifier = 'study_mixed'
                UNION ALL
                SELECT sample_unique_id FROM mutation_panel_gene_coverage
                WHERE cancer_study_identifier = 'study_mixed'
                  AND hugo_gene_symbol = '{r["hugo_gene_symbol"]}'
                  AND sample_unique_id IN (
                      SELECT sample_unique_id FROM mutation_panel_gene_coverage
                      WHERE cancer_study_identifier = 'study_mixed' AND hugo_gene_symbol = 'TP53'))
        """,
        )[0]["n"]
        assert r["profiled"] == expected, r


# --- frequency_pct: tool (Python) vs recipe (ClickHouse ROUND) ----------------


def test_frequency_pct_matches_clickhouse_round_on_boundaries(ch):
    """Every pair with profiled <= 400 plus exact .x5 halves up to 20000."""
    client, _ = ch
    rows = _rows(
        client,
        """
        SELECT a, b, ROUND(a * 100.0 / NULLIF(b, 0), 1) AS r FROM (
            SELECT b, a FROM (SELECT number AS b FROM numbers(1, 400))
            ARRAY JOIN range(toUInt64(b + 1)) AS a
            UNION ALL
            SELECT b, intDiv((2 * k + 1) * b, 2000) AS a
            FROM (SELECT number * 40 AS b FROM numbers(1, 500)) ARRAY JOIN range(1000) AS k
            WHERE ((2 * k + 1) * b) % 2000 = 0
        )
    """,
    )
    mismatches = [r for r in rows if domain_tools._frequency_pct(r["a"], r["b"]) != r["r"]]
    assert len(rows) > 80000 and mismatches == []


@pytest.mark.parametrize("preference", ["pref_all", "pref_mixed", "pan_cancer_tcga"])
@pytest.mark.parametrize("alteration", ALTERATIONS)
def test_gene_frequency_tool_equals_recipe_including_frequency_pct(
    ch, tool_db, preference, alteration
):
    client, _ = ch
    for gene in fx.GENES:
        recipe = {
            r["cancer_type"]: (r["altered_samples"], r["profiled_samples"], r["frequency_pct"])
            for r in _rows(
                client,
                f"""
                SELECT * FROM gene_alteration_frequency_by_cancer_type(
                    preference='{preference}', gene='{gene}', alteration='{alteration}')
            """,
            )
        }
        out = domain_tools.get_gene_frequency_by_cancer_type.fn(gene, alteration, 100, preference)
        tool = {
            r["cancer_type"]: (r["altered_samples"], r["profiled_samples"], r["frequency_pct"])
            for r in _records(out)
        }
        assert tool == recipe, (gene, out["source"])


@pytest.mark.parametrize("study", STUDIES_WITH_DATA)
def test_top_altered_genes_tool_equals_recipe_including_frequency_pct(ch, tool_db, study):
    client, _ = ch
    recipe = [
        (r["hugo_gene_symbol"], r["altered_samples"], r["profiled_samples"], r["frequency_pct"])
        for r in _rows(
            client, f"SELECT * FROM top_mutated_genes_in_study(study='{study}', top_n=100)"
        )
    ]
    out = domain_tools.get_top_altered_genes.fn(study, "mutation", 100)
    tool = [
        (r["hugo_gene_symbol"], r["altered_samples"], r["profiled_samples"], r["frequency_pct"])
        for r in _records(out)
    ]
    assert out["source"] == "precomputed" and tool == recipe


# --- tools: precomputed path == live fallback path ---------------------------

TOOL_GENES = ["TP53", "BRAF", "ALK", "C1orf112", "tp53"]


def _without_source(result):
    return {k: v for k, v in result.items() if k not in ("source", "built_at", "fallback_reason")}


def _tool_calls():
    calls = []
    for study in fx.STUDIES.values():
        calls.append((domain_tools.get_profiled_counts, (study,)))
        for alt in ["any"] + ALTERATIONS:
            calls.append((domain_tools.get_top_altered_genes, (study, alt, 100)))
            for gene in TOOL_GENES:
                calls.append((domain_tools.get_alteration_frequency, (gene, study, alt)))
    for pref in ("pref_all", "pref_mixed", "pan_cancer_tcga"):
        for alt in ["any"] + ALTERATIONS:
            for gene in ("TP53", "KRAS", "ALK"):
                calls.append(
                    (domain_tools.get_gene_frequency_by_cancer_type, (gene, alt, 100, pref))
                )
    return calls


def test_tools_return_identical_results_from_precomputed_and_live(ch, tool_db):
    client, _ = ch
    calls = _tool_calls()
    precomputed = [fn.fn(*args) for fn, args in calls]

    for t in AGGREGATE_TABLES:
        client.command(f"RENAME TABLE {t} TO {t}__hidden")
    try:
        live = [fn.fn(*args) for fn, args in calls]
    finally:
        for t in AGGREGATE_TABLES:
            client.command(f"RENAME TABLE {t}__hidden TO {t}")

    assert all(r.get("source") == "live" for r in live), "fallback path not exercised"
    assert sum(r.get("source") == "precomputed" for r in precomputed) > len(calls) // 2
    for (fn, args), p, v in zip(calls, precomputed, live, strict=True):
        assert "error_message" not in p, (fn.name, args, p)
        assert _without_source(p) == _without_source(v), (fn.name, args)


def test_alteration_frequency_matches_oracle_including_zero_rows(ch, tool_db):
    _, tables = ch
    expected = fx.oracle_study_counts(tables)
    for study in STUDIES_WITH_DATA:
        for gene in fx.GENES:
            for alt in ALTERATIONS:
                out = domain_tools.get_alteration_frequency.fn(gene, study, alt)
                row = _records(out)[0]
                altered, profiled, _ = expected.get(
                    (study, gene, alt), (0, fx.oracle_study_profiled(tables, study, alt, gene), 0)
                )
                assert (row["altered_samples"], row["profiled_samples"]) == (altered, profiled), (
                    study,
                    gene,
                    alt,
                    out["source"],
                )
                assert row["frequency_pct"] == domain_tools._frequency_pct(altered, profiled)


def test_query_labels_tag_each_path(ch, tool_db):
    domain_tools.get_top_altered_genes.fn("study_wes", "mutation", 5)
    domain_tools.get_alteration_frequency.fn("BRAF", "study_panel", "mutation")  # 0 altered
    assert "domain_tools.top_altered_genes.precomputed" in tool_db
    assert "domain_tools.alteration_frequency.live" in tool_db


def test_unknown_gene_and_study_are_errors_not_zeros(ch, tool_db):
    gene_miss = domain_tools.get_alteration_frequency.fn("NOTAGENE1", "study_wes", "any")
    study_miss = domain_tools.get_alteration_frequency.fn("TP53", "no_such_study", "any")
    assert "not in the gene table" in gene_miss["error_message"]
    assert "did not match any study" in study_miss["error_message"]


def test_lowercase_gene_resolves(ch, tool_db):
    out = domain_tools.get_alteration_frequency.fn("tp53", "study_wes", "mutation")
    assert out["gene"] == "TP53" and _records(out)[0]["altered_samples"] > 0


# --- real apply order: scripts/apply_sql.sh with the public-portal cohorts ---

# Fixture studies renamed to identifiers the real public-portal preference
# file (and sql/3's pan_cancer_tcga pattern) select.
PUBLIC_NAMES = {
    "study_wes": "brca_tcga_pan_can_atlas_2018",  # pan_cancer_tcga + all_studies_non_redundant
    "study_panel": "msk_impact_50k_2026",  # large_genomic_cohort
    "study_mixed": "msk_chord_2024",  # treatment_outcomes
    "study_tiny": "tiny_study_2020",  # in no cohort
}
PUBLIC_COHORTS = [
    "pan_cancer_tcga",
    "all_studies_non_redundant",
    "large_genomic_cohort",
    "treatment_outcomes",
]


def _stage_real_sql_dir(root: Path, aggregates_dir: str) -> Path:
    """The real sql files apply_sql.sh reads, minus files that need the full cBioPortal schema.

    aggregates_dir "final" is this branch's layout; "" puts the aggregate
    file in the portable phase (the old layout) to prove it then misses the
    portal-specific cohorts.
    """
    sql = root / "sql"
    (sql / "portal-specific" / "public-portal").mkdir(parents=True)
    for name in ("3-add-cancer-study-query-preferences.sql", "4-mutation-frequency-views.sql"):
        shutil.copy(SQL_DIR / name, sql / name)
    shutil.copy(
        SQL_DIR / "portal-specific" / "public-portal" / "0-preferences.sql",
        sql / "portal-specific" / "public-portal" / "0-preferences.sql",
    )
    if aggregates_dir:
        (sql / aggregates_dir).mkdir()
        shutil.copy(AGGREGATES_SQL, sql / aggregates_dir / AGGREGATES_SQL.name)
    else:
        shutil.copy(AGGREGATES_SQL, sql / "8-precomputed-aggregates.sql")
    return sql


def _run_apply_sql(sql_dir: Path, database: str) -> str:
    url = urlparse(URL)
    env = dict(
        os.environ,
        CLICKHOUSE_HOST=url.hostname,
        CLICKHOUSE_PORT=NATIVE_PORT,
        CLICKHOUSE_SECURE="false",
        CLICKHOUSE_DATABASE=database,
        CLICKHOUSE_ADMIN_USER=url.username or "default",
        CLICKHOUSE_ADMIN_PASSWORD=url.password or "",
        SQL_DIR=str(sql_dir),
    )
    done = subprocess.run(
        ["bash", str(REPO / "scripts" / "apply_sql.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


needs_client = pytest.mark.skipif(
    not NATIVE_PORT or not shutil.which("clickhouse-client"),
    reason="needs CBIOPORTAL_MCP_TEST_CLICKHOUSE_NATIVE_PORT and clickhouse-client on PATH",
)


@pytest.fixture(params=["final", ""], ids=["final-phase", "portable-phase"])
def applied(request, tmp_path):
    admin = _client()
    db = f"mcp_aggorder_{uuid.uuid4().hex[:10]}"
    admin.command(f"CREATE DATABASE {db}")
    try:
        client = _client(database=db)
        _load(client, fx.generate(names=PUBLIC_NAMES, preferences=False))
        log = _run_apply_sql(_stage_real_sql_dir(tmp_path, request.param), db)
        yield request.param, client, log
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {db}")


@needs_client
def test_real_apply_order_precomputes_every_public_cohort(applied, monkeypatch):
    layout, client, log = applied
    cohorts = {
        r["preference_name"]
        for r in _rows(
            client, "SELECT DISTINCT preference_name FROM cancer_study_query_preferences"
        )
    }
    assert set(PUBLIC_COHORTS) <= cohorts, cohorts  # the real files created them
    built = {
        r["preference_name"]
        for r in _rows(
            client, "SELECT DISTINCT preference_name FROM cancer_type_gene_alteration_counts"
        )
    }
    _point_tools_at(client, monkeypatch)
    sources = {
        pref: domain_tools.get_gene_frequency_by_cancer_type.fn("TP53", "mutation", 10, pref)[
            "source"
        ]
        for pref in PUBLIC_COHORTS
    }
    if layout == "final":
        assert log.index("apply  final/") > log.index("apply  portal-specific/")
        assert built == cohorts
        assert sources == {pref: "precomputed" for pref in PUBLIC_COHORTS}
    else:
        # The old layout: aggregates built before the portal-specific phase
        # see only sql/3's pan_cancer_tcga, so the public cohorts fall back.
        assert built == {"pan_cancer_tcga"}
        assert sources["pan_cancer_tcga"] == "precomputed"
        assert {sources[p] for p in PUBLIC_COHORTS[1:]} == {"live"}


@needs_client
@pytest.mark.parametrize("applied", ["final"], indirect=True, ids=["final-phase"])
def test_real_apply_order_precomputed_equals_live_for_public_cohorts(applied, monkeypatch):
    _, client, _ = applied
    _point_tools_at(client, monkeypatch)
    calls = [
        (gene, alt, pref)
        for pref in PUBLIC_COHORTS
        for alt in ["any"] + ALTERATIONS
        for gene in ("TP53", "KRAS", "ALK")
    ]
    precomputed = [
        domain_tools.get_gene_frequency_by_cancer_type.fn(g, a, 100, p) for g, a, p in calls
    ]
    client.command("RENAME TABLE cancer_type_gene_alteration_counts TO ctgac__hidden")
    try:
        live = [
            domain_tools.get_gene_frequency_by_cancer_type.fn(g, a, 100, p) for g, a, p in calls
        ]
    finally:
        client.command("RENAME TABLE ctgac__hidden TO cancer_type_gene_alteration_counts")
    # A call with no qualifying cancer type falls back to live on both passes.
    assert sum(p["source"] == "precomputed" for p in precomputed) > len(calls) // 2
    for args, p, v in zip(calls, precomputed, live, strict=True):
        assert v["source"] == "live", args
        assert _without_source(p) == _without_source(v), args
