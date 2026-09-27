"""Precomputed aggregates == live recipes, on a real ClickHouse.

Opt-in: set CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL to a server you can create a
scratch database on, e.g.

    docker run -d --name ch -p 28123:8123 -e CLICKHOUSE_USER=admin \\
        -e CLICKHOUSE_PASSWORD=admin clickhouse/clickhouse-server:24.8
    CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL=http://admin:admin@localhost:28123 \\
        uv run pytest tests/test_domain_tools_equivalence.py

The module loads the synthetic fixture (tests/aggregate_fixture.py) into a
fresh database, applies sql/4-mutation-frequency-views.sql and
sql/8-precomputed-aggregates.sql exactly as the clone job would, and checks
three independent implementations against each other: the sql/8 tables, the
existing recipe views / the tools' live-fallback SQL, and a pure-Python
oracle over the generated rows.
"""

import os
import uuid
from pathlib import Path

import aggregate_fixture as fx
import pytest

from cbioportal_mcp import domain_tools, server

URL = os.getenv("CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL not set")

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
ALTERATIONS = ["mutation", "amplification", "deep_deletion", "structural_variant"]
AGGREGATE_TABLES = (
    "study_gene_alteration_counts",
    "cancer_type_gene_alteration_counts",
    "study_profiled_counts",
)


def _statements(path: Path) -> list[str]:
    """Split a sql/*.sql file the way clickhouse-client --queries-file would."""
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


@pytest.fixture(scope="module")
def ch():
    admin = _client()
    db = f"mcp_aggtest_{uuid.uuid4().hex[:10]}"
    admin.command(f"CREATE DATABASE {db}")
    client = _client(database=db)
    try:
        tables = fx.generate()
        for ddl in fx.DDL:
            client.command(ddl)
        for name, rows in tables.items():
            columns = [c.strip() for c in fx.INSERT_COLUMNS[name].split(",")]
            client.insert(name, rows, column_names=columns)
        for f in ("4-mutation-frequency-views.sql", "8-precomputed-aggregates.sql"):
            for stmt in _statements(SQL_DIR / f):
                client.command(stmt)
        yield client, tables
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {db}")


def _rows(client, sql):
    res = client.query(sql)
    return [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]


def _union(parts: list[str]) -> str:
    return " UNION ALL ".join(f"SELECT * FROM ({p})" for p in parts)


@pytest.fixture
def tool_db(ch, monkeypatch):
    """Point the tools' run_select_query at the scratch database."""
    client, _ = ch
    labels = []

    def run_select_query(query, *, query_label, max_rows=None):
        labels.append(query_label)
        res = client.query(query)
        return server.zip_select_query_result(
            {"columns": res.column_names, "rows": res.result_rows}
        )

    monkeypatch.setattr(server, "run_select_query", run_select_query)
    return labels


# --- sql/8 tables vs the pure-Python oracle ---------------------------------


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


# --- sql/8 tables vs the existing recipe views (sql/4) -----------------------


@pytest.mark.parametrize("preference", sorted(fx.PREFERENCES))
@pytest.mark.parametrize("alteration", ALTERATIONS)
def test_cancer_type_table_equals_gene_alteration_frequency_by_cancer_type(
    ch, preference, alteration
):
    client, _ = ch
    recipe = _rows(
        client,
        _union(
            [
                f"""
        SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
        FROM gene_alteration_frequency_by_cancer_type(
            preference='{preference}', gene='{gene}', alteration='{alteration}')
    """
                for gene in fx.GENES
            ]
        ),
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = '{preference}' AND alteration_type = '{alteration}'
          AND profiled_samples >= 50
    """,
    )
    # Some combinations are legitimately empty (e.g. SV in pref_mixed: fewer
    # than 50 SV-profiled samples); test_fixture_exercises_the_edge_cases
    # guards against the whole comparison going vacuous.
    key = lambda r: (r["gene"], r["cancer_type"])  # noqa: E731
    assert sorted(table, key=key) == sorted(recipe, key=key)


@pytest.mark.parametrize("preference", sorted(fx.PREFERENCES))
def test_cancer_type_table_equals_gene_mutation_frequency_by_cancer_type(ch, preference):
    client, _ = ch
    recipe = _rows(
        client,
        _union(
            [
                f"""
        SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
        FROM gene_mutation_frequency_by_cancer_type(preference='{preference}', gene='{gene}')
    """
                for gene in fx.GENES
            ]
        ),
    )
    table = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = '{preference}' AND alteration_type = 'mutation'
          AND profiled_samples >= 50
    """,
    )
    key = lambda r: (r["gene"], r["cancer_type"])  # noqa: E731
    assert sorted(table, key=key) == sorted(recipe, key=key)


def test_single_study_preference_equals_gene_mutation_frequency_in_study(ch):
    client, _ = ch
    recipe = _rows(
        client,
        _union(
            [
                f"""
        SELECT '{gene}' AS gene, cancer_type, altered_samples, profiled_samples
        FROM gene_mutation_frequency_in_study(study='study_panel', gene='{gene}')
    """
                for gene in fx.GENES
            ]
        ),
    )
    table = _rows(
        client,
        """
        SELECT hugo_gene_symbol AS gene, cancer_type, altered_samples, profiled_samples
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = 'pref_panel' AND alteration_type = 'mutation'
          AND profiled_samples >= 50
    """,
    )
    key = lambda r: (r["gene"], r["cancer_type"])  # noqa: E731
    assert sorted(table, key=key) == sorted(recipe, key=key)


# study_wes / study_panel have no sample that is WES AND on a panel for one
# profile type, so the top_*_genes_in_study views (which add WES + panel
# counts) must match exactly there.
@pytest.mark.parametrize("study", ["study_wes", "study_panel"])
def test_study_table_equals_top_mutated_genes_in_study(ch, study):
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, total_mutation_events
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
    assert recipe and table == recipe


@pytest.mark.parametrize("study", ["study_wes", "study_panel", "study_mixed"])
def test_study_table_equals_top_cna_genes_in_study(ch, study):
    """Includes study_mixed: log2-only samples must not enter the CNA denominator."""
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol,
               if(cna_type = 'AMP', 'amplification', 'deep_deletion') AS alteration_type,
               altered_samples, profiled_samples
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
    key = lambda r: (r["hugo_gene_symbol"], r["alteration_type"])  # noqa: E731
    assert recipe and sorted(table, key=key) == sorted(recipe, key=key)


@pytest.mark.parametrize("study", ["study_wes", "study_panel", "study_mixed"])
def test_study_table_equals_top_sv_genes_in_study(ch, study):
    client, _ = ch
    recipe = _rows(
        client,
        f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, total_sv_events
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
    assert recipe and table == recipe


def test_top_mutated_genes_in_study_double_counts_wes_plus_panel_samples(ch):
    """Documents why sql/8 uses the union denominator of gene_*_frequency_* instead.

    top_mutated_genes_in_study adds WES + panel counts, so a sample that is WES
    for one mutation profile and on PANEL_A for another is counted twice for the
    PANEL_A genes. The union-based recipes (and sql/8) count it once.
    """
    client, _ = ch
    recipe = {
        r["hugo_gene_symbol"]: (r["altered_samples"], r["profiled_samples"])
        for r in _rows(
            client, "SELECT * FROM top_mutated_genes_in_study(study='study_mixed', top_n=1000)"
        )
    }
    table = {
        r["hugo_gene_symbol"]: (r["altered_samples"], r["profiled_samples"])
        for r in _rows(
            client,
            """
            SELECT hugo_gene_symbol, altered_samples, profiled_samples
            FROM study_gene_alteration_counts
            WHERE cancer_study_identifier = 'study_mixed' AND alteration_type = 'mutation'
        """,
        )
    }
    assert table.keys() == recipe.keys()
    panel_a = fx.PANEL_GENES["PANEL_A"]
    for gene, (altered, profiled) in table.items():
        assert recipe[gene][0] == altered, gene  # numerators always agree
        if gene in panel_a:
            assert recipe[gene][1] > profiled, gene
        else:
            assert recipe[gene][1] == profiled, gene


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
    for study in ("study_wes", "study_panel", "study_mixed"):
        for gene in fx.GENES:
            for alt in ALTERATIONS:
                out = domain_tools.get_alteration_frequency.fn(gene, study, alt)
                row = out["rows"][0]
                altered, profiled, _ = expected.get(
                    (study, gene, alt), (0, fx.oracle_study_profiled(tables, study, alt, gene), 0)
                )
                assert (row["altered_samples"], row["profiled_samples"]) == (altered, profiled), (
                    study,
                    gene,
                    alt,
                    out["source"],
                )


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
    assert out["gene"] == "TP53" and out["rows"][0]["altered_samples"] > 0
