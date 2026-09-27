"""Unit tests for the precomputed-aggregate tools (no database).

The database-backed equivalence proof lives in test_domain_tools_equivalence.py.
"""

import asyncio
import re
from pathlib import Path

import pytest

from cbioportal_mcp import domain_tools, server

REPO = Path(__file__).resolve().parent.parent
SQL_8 = REPO / "sql" / "8-precomputed-aggregates.sql"
TOOL_NAMES = {
    "get_alteration_frequency",
    "get_top_altered_genes",
    "get_gene_frequency_by_cancer_type",
    "get_profiled_counts",
}


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeDB:
    """Records (label, sql) and answers from a label -> rows / exception map."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, query, *, query_label, max_rows=None):
        self.calls.append((query_label, _norm(query)))
        answer = self.answers.get(query_label, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def labels(self):
        return [label for label, _ in self.calls]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


# --- registration ------------------------------------------------------------


def test_tools_are_registered_on_the_server():
    tools = asyncio.run(server.mcp.get_tools())
    assert TOOL_NAMES <= set(tools)


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_descriptions_are_short_and_steer_away_from_hand_written_sql(name):
    description = _norm(asyncio.run(server.mcp.get_tools())[name].description)
    assert "Prefer this over hand-written SQL" in description
    assert len(description) < 700


# --- validation / injection --------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: domain_tools.get_alteration_frequency.fn("TP53", "x' OR 1=1 --"),
        lambda: domain_tools.get_alteration_frequency.fn("TP53'; DROP TABLE gene", "s"),
        lambda: domain_tools.get_alteration_frequency.fn("TP53", "s", "fusion"),
        lambda: domain_tools.get_top_altered_genes.fn("", "mutation"),
        lambda: domain_tools.get_top_altered_genes.fn("s", "amp"),
        lambda: domain_tools.get_gene_frequency_by_cancer_type.fn("TP53", "mutation", 10, "X'"),
        lambda: domain_tools.get_gene_frequency_by_cancer_type.fn("", "mutation"),
        lambda: domain_tools.get_profiled_counts.fn("a b"),
    ],
)
def test_invalid_input_is_rejected_before_any_query(db, call):
    out = call()
    assert "error_message" in out
    assert db.calls == []


def test_sql_str_refuses_anything_outside_the_allow_list():
    for bad in ("a'b", "a\\b", "a b", "", "x" * 200):
        with pytest.raises(ValueError):
            domain_tools._sql_str(bad)
    assert domain_tools._sql_str("NKX2-1") == "'NKX2-1'"


def test_gene_and_study_candidates_cover_case_variants():
    assert domain_tools._gene_candidates("tp53") == ("tp53", "TP53")
    assert domain_tools._gene_candidates("C1orf112") == ("C1orf112", "C1ORF112")
    assert domain_tools._gene_candidates("KRAS") == ("KRAS",)
    assert domain_tools._study_candidates("BRCA_TCGA") == ("BRCA_TCGA", "brca_tcga")


@pytest.mark.parametrize("given,limit", [(0, 1), (-5, 1), (7, 7), (1000, 100)])
def test_top_n_is_clamped(db, given, limit):
    domain_tools.get_top_altered_genes.fn("study_a", "mutation", given)
    assert f"LIMIT {limit}" in db.calls[0][1]


def test_alteration_type_is_case_insensitive(db):
    domain_tools.get_top_altered_genes.fn("study_a", "Amplification")
    assert "alteration_type = 'amplification'" in db.calls[0][1]


# --- SQL building ------------------------------------------------------------


def test_precomputed_queries_read_the_aggregate_tables():
    assert "FROM study_gene_alteration_counts" in _norm(
        domain_tools.build_alteration_frequency_precomputed_sql(("TP53",), ("s",), ["any"])
    )
    assert "FROM study_gene_alteration_counts" in _norm(
        domain_tools.build_top_altered_genes_precomputed_sql(("s",), "mutation", 5)
    )
    by_type = _norm(
        domain_tools.build_frequency_by_cancer_type_precomputed_sql(("TP53",), "mutation", "p", 5)
    )
    assert "FROM cancer_type_gene_alteration_counts" in by_type
    assert "profiled_samples >= 50" in by_type
    assert "FROM study_profiled_counts" in _norm(
        domain_tools.build_profiled_counts_precomputed_sql(("s",))
    )


def test_single_study_live_sql_uses_discrete_cna_and_drops_uncalled_svs():
    for sql in (
        domain_tools.build_alteration_frequency_live_sql(("TP53",), ("s",)),
        domain_tools.build_top_altered_genes_live_sql(("s",), "any", 5),
        domain_tools.build_profiled_counts_live_sql(("s",)),
    ):
        assert "datatype = 'DISCRETE'" in _norm(sql)
    sv = _norm(domain_tools.build_top_altered_genes_live_sql(("s",), "structural_variant", 5))
    assert "variant_type = 'structural_variant' AND mutation_status != 'UNCALLED'" in sv
    # Mutation-only top-N must not drag the CNA profile filter in.
    assert "DISCRETE" not in domain_tools.build_top_altered_genes_live_sql(("s",), "mutation", 5)


def test_cross_study_live_sql_reuses_the_recipe_view():
    sql = _norm(
        domain_tools.build_frequency_by_cancer_type_live_sql(
            ("tp53", "TP53"), "amplification", "pan_cancer_tcga", 10
        )
    )
    assert sql.count("FROM gene_alteration_frequency_by_cancer_type(") == 2
    assert "alteration='amplification'" in sql and "LIMIT 10" in sql


def test_cross_study_any_keeps_the_recipe_rules():
    """No DISCRETE restriction and no UNCALLED-SV filter: same as the recipe view."""
    sql = _norm(domain_tools.build_frequency_by_cancer_type_live_sql(("TP53",), "any", "p", 10))
    assert "DISCRETE" not in sql
    assert "(variant_type = 'structural_variant')" in sql
    assert "profiled_samples >= 50" in sql


def test_frequency_pct_rounding_and_zero_denominator():
    assert domain_tools._frequency_pct(1, 3) == 33.3
    assert domain_tools._frequency_pct(0, 0) is None


# --- fallback ----------------------------------------------------------------

LABEL = "domain_tools.top_altered_genes"


def test_precomputed_hit_does_not_run_live_sql(db):
    db.answers[f"{LABEL}.precomputed"] = [
        {
            "hugo_gene_symbol": "TP53",
            "altered_samples": 30,
            "profiled_samples": 100,
            "built_at": "2026-09-26 13:05:00",
        }
    ]
    out = domain_tools.get_top_altered_genes.fn("study_a", "mutation", 5)

    assert db.labels == [f"{LABEL}.precomputed"]
    assert out["source"] == "precomputed" and out["built_at"] == "2026-09-26 13:05:00"
    assert out["rows"] == [
        {
            "hugo_gene_symbol": "TP53",
            "altered_samples": 30,
            "profiled_samples": 100,
            "frequency_pct": 30.0,
        }
    ]
    assert "fallback_reason" not in out and "denominator" in out["provenance"]


def test_missing_table_falls_back_to_live_sql_with_a_flag(db):
    db.answers[f"{LABEL}.precomputed"] = RuntimeError(
        "Table study_gene_alteration_counts doesn't exist"
    )
    db.answers[f"{LABEL}.live"] = [
        {"hugo_gene_symbol": "KRAS", "altered_samples": 2, "profiled_samples": 8}
    ]
    out = domain_tools.get_top_altered_genes.fn("study_a", "mutation", 5)

    assert db.labels == [f"{LABEL}.precomputed", f"{LABEL}.live"]
    assert out["source"] == "live"
    assert "unavailable" in out["fallback_reason"]
    assert out["rows"][0]["frequency_pct"] == 25.0


def test_empty_table_falls_back_to_live_sql(db):
    out = domain_tools.get_top_altered_genes.fn("study_a", "mutation", 5)
    assert db.labels == [f"{LABEL}.precomputed", f"{LABEL}.live"]
    assert out["source"] == "live" and "no precomputed rows" in out["fallback_reason"]
    assert out["rows"] == [] and "note" in out


def test_live_failure_is_reported_not_raised(db):
    db.answers[f"{LABEL}.precomputed"] = RuntimeError("boom")
    db.answers[f"{LABEL}.live"] = RuntimeError("also boom")
    assert domain_tools.get_top_altered_genes.fn("study_a")["error_message"] == "also boom"


# --- get_alteration_frequency ------------------------------------------------

AF = "domain_tools.alteration_frequency"


def _pre(alteration_type, altered, profiled):
    return {
        "hugo_gene_symbol": "TP53",
        "cancer_study_identifier": "study_a",
        "alteration_type": alteration_type,
        "altered_samples": altered,
        "profiled_samples": profiled,
        "built_at": "2026-09-26 13:05:00",
    }


def test_alteration_frequency_any_returns_breakdown_from_precomputed(db):
    db.answers[f"{AF}.precomputed"] = [_pre("any", 40, 100), _pre("mutation", 35, 100)]
    out = domain_tools.get_alteration_frequency.fn("tp53", "study_a")

    assert db.labels == [f"{AF}.precomputed"]
    assert out["gene"] == "TP53" and out["source"] == "precomputed"
    assert [r["alteration_type"] for r in out["rows"]] == ["any", "mutation"]
    assert out["rows"][0]["frequency_pct"] == 40.0


def test_alteration_frequency_unaltered_type_falls_back_for_the_denominator(db):
    """No stored row = 0 altered; only the live query knows how many were profiled."""
    db.answers[f"{AF}.precomputed"] = [_pre("any", 40, 100), _pre("mutation", 35, 100)]
    db.answers[f"{AF}.live"] = [
        {
            "matched_genes": ["TP53"],
            "matched_studies": ["study_a"],
            "altered_amplification": 0,
            "profiled_COPY_NUMBER_ALTERATION": 80,
        }
    ]
    out = domain_tools.get_alteration_frequency.fn("TP53", "study_a", "amplification")

    assert db.labels == [f"{AF}.precomputed", f"{AF}.live"]
    assert out["source"] == "live"
    assert out["rows"] == [
        {
            "alteration_type": "amplification",
            "altered_samples": 0,
            "profiled_samples": 80,
            "frequency_pct": 0.0,
        }
    ]


def test_alteration_frequency_unknown_gene_is_an_error_not_zero(db):
    db.answers[f"{AF}.live"] = [{"matched_genes": [], "matched_studies": ["study_a"]}]
    out = domain_tools.get_alteration_frequency.fn("NOTAGENE", "study_a")
    assert "not in the gene table" in out["error_message"]


def test_alteration_frequency_unknown_study_uses_the_deployment_message(db, monkeypatch):
    monkeypatch.setattr(server, "_similar_study_identifiers", lambda study_id: [])
    db.answers[f"{AF}.live"] = [{"matched_genes": ["TP53"], "matched_studies": []}]
    out = domain_tools.get_alteration_frequency.fn("TP53", "nope_2020")
    assert "did not match any study" in out["error_message"]


# --- get_profiled_counts / by cancer type -------------------------------------


def test_profiled_counts_unknown_study(db, monkeypatch):
    monkeypatch.setattr(server, "_similar_study_identifiers", lambda study_id: [])
    out = domain_tools.get_profiled_counts.fn("nope_2020")
    assert "did not match any study" in out["error_message"]
    assert db.labels == [
        "domain_tools.profiled_counts.precomputed",
        "domain_tools.profiled_counts.live",
    ]


def test_gene_frequency_by_cancer_type_shapes_rows(db):
    db.answers["domain_tools.gene_frequency_by_cancer_type.precomputed"] = [
        {
            "cancer_type": "Breast Cancer",
            "hugo_gene_symbol": "TP53",
            "altered_samples": 50,
            "profiled_samples": 200,
            "built_at": "2026-09-26 13:05:00",
        }
    ]
    out = domain_tools.get_gene_frequency_by_cancer_type.fn("tp53")
    assert out["gene"] == "TP53" and out["preference"] == "pan_cancer_tcga"
    assert out["rows"][0]["frequency_pct"] == 25.0
    assert "< 50 profiled samples omitted" in out["provenance"]


# --- sql/8 file hygiene ------------------------------------------------------


def _sql8_without_comments():
    return re.sub(r"--[^\n]*", "", SQL_8.read_text())


def test_every_table_is_dropped_before_create():
    sql = SQL_8.read_text()
    tables = re.findall(r"^CREATE TABLE (\w+)", sql, re.M)
    assert set(tables) == {
        "study_gene_alteration_counts",
        "cancer_type_gene_alteration_counts",
        "study_profiled_counts",
    }
    for table in tables:
        assert f"DROP TABLE IF EXISTS {table};" in sql, table


def test_sql8_does_not_reference_database_names():
    assert not re.search(r"\b(FROM|JOIN|INTO|TABLE)\s+\w+\.\w+", _sql8_without_comments())


def test_sql8_has_no_semicolons_inside_string_literals():
    """Keeps the file safe for naive ;-splitting (tests, ad-hoc tooling)."""
    for literal in re.findall(r"'(?:[^']|'')*'", _sql8_without_comments()):
        assert ";" not in literal, literal


def test_sql8_is_listed_in_sql_readme():
    assert "8-precomputed-aggregates.sql" in (REPO / "sql" / "README.md").read_text()
