"""Live regression for HIGH_STRESS_TEST.md (T1-T14) against a real ClickHouse.

Skipped unless ``CLICKHOUSE_HOST`` is set (export the variables the server reads:
CLICKHOUSE_HOST / PORT / USER / PASSWORD / SECURE / DATABASE).

Each STRESS question used to fail by design: the tool either could not express the
request, or answered a different question without saying so. These tests pin the tool
layer of the fix -- the analysis that was asked for is now computed, and the naive
call that used to mislead now flags the gap -- so the model has a correct payload to
quote. TCGA PanCancer Atlas is frozen, so its counts are asserted exactly (numbers
from the LLM-prepped public clone on 2026-09-13).

Run: ``uv run --python 3.12 --extra dev pytest tests/test_high_stress_live.py -q``
"""

import os

import pytest

from cbioportal_mcp import server

pytestmark = pytest.mark.skipif(
    not os.getenv("CLICKHOUSE_HOST"), reason="live ClickHouse credentials not in the environment"
)

TCGA = "pan_cancer_tcga"


def _ok(payload):
    assert not payload.get("error"), payload.get("error")
    return payload


def _groups(payload):
    return {g["name"]: g["n_patients"] for g in payload["groups"]}


# --- survival_curve ------------------------------------------------------------------


def test_t1_breast_cancer_in_msk_chord_is_filterable_and_the_unfiltered_run_says_so():
    whole = _ok(server.survival_curve("msk_chord_2024"))
    assert whole["cohort"]["unfiltered"] is True
    assert "cancer types" in whole["cohort"]["warning"]
    breast = _ok(server.survival_curve("msk_chord_2024", cohort={"CANCER_TYPE": ["Breast Cancer"]}))
    assert breast["cohort"]["filter"] == {"CANCER_TYPE": ["Breast Cancer"]}
    assert 1000 < breast["cohort"]["n_patients"] < whole["cohort"]["n_patients"]


def test_t2_co_mutation_groups_across_tcga_with_stratification():
    misuse = server.survival_curve(TCGA, group_by_gene="KRAS")
    assert "Pass preference='pan_cancer_tcga'" in misuse["error"]

    naive = _ok(server.survival_curve(preference=TCGA, group_by_gene="KRAS"))
    assert naive["stratification"] is None
    assert any("NOT adjusted for cancer type" in w for w in naive["warnings"])

    p = _ok(
        server.survival_curve(
            preference=TCGA,
            stratify_by="CANCER_TYPE",
            groups=[
                {"name": "TP53+KRAS", "altered": ["TP53: MUT", "KRAS: MUT"]},
                {"name": "KRAS only", "altered": "KRAS: MUT", "unaltered": "TP53: MUT"},
            ],
        )
    )
    assert _groups(p) == {"TP53+KRAS": 331, "KRAS only": 418}
    assert p["grouping"]["type"] == "custom"
    assert p["stats"]["test"] == "stratified log-rank"
    assert p["stratification"]["by"] == "CANCER_TYPE"
    assert p["stats_unstratified"]["p_value"] == pytest.approx(0.025483, abs=1e-5)
    balance = sum(g["observed"] - g["expected"] for g in p["stats"]["group_observed_expected"])
    assert balance == pytest.approx(0.0, abs=1e-6)


def test_t3_survival_by_egfr_expression_quartiles():
    p = _ok(
        server.survival_curve(
            "luad_tcga_pan_can_atlas_2018",
            group_by_expression={"gene": "EGFR", "split": "top_vs_bottom_quartile"},
        )
    )
    g = p["grouping"]
    assert g["type"] == "expression" and g["profiles"] == "rna_seq_v2_mrna"
    assert g["cutoffs"]["q1"] < g["cutoffs"]["q3"]
    assert _groups(p) == {"top quartile": 125, "bottom quartile": 126}
    assert p["stats"]["p_value"] is not None


def test_t4_tp53_codon_region_rest_and_wild_type():
    p = _ok(
        server.survival_curve(
            preference=TCGA,
            stratify_by="CANCER_TYPE",
            groups=[
                {"name": "TP53 codons 1-40", "altered": "TP53: MUT = (1-40)"},
                {"name": "TP53 codons 41+", "altered": "TP53: MUT = (41-)"},
                {"name": "TP53 wild-type", "unaltered": "TP53: MUT"},
            ],
        )
    )
    assert _groups(p) == {"TP53 codons 1-40": 56, "TP53 codons 41+": 3715, "TP53 wild-type": 6509}
    assert p["grouping"]["n_overlapping_excluded"] == 17
    assert p["stats"]["df"] == 2


# --- alteration_cooccurrence ------------------------------------------------------------


def test_t5_pathway_level_exclusivity_in_pancreatic_adenocarcinoma():
    p = _ok(
        server.alteration_cooccurrence(
            "paad_tcga_pan_can_atlas_2018",
            tracks=[
                '["Cell cycle checkpoint" CDKN2A CDK4 CDK6 CCND1 RB1]',
                '["Homologous recombination repair" BRCA1 BRCA2 PALB2 ATM RAD51C]',
            ],
        )
    )
    assert p["genes"] == ["Cell cycle checkpoint", "Homologous recombination repair"]
    assert [t["merged"] for t in p["tracks"]] == [True, True]
    (pair,) = p["pairs"]
    assert pair["n_profiled"] == 179 and pair["n_both"] == 9


def test_t6_tumour_type_confounding_is_flagged_then_adjusted():
    genes = ["TP53", "KRAS", "PIK3CA", "APC", "EGFR"]
    crude = _ok(server.alteration_cooccurrence("msk_impact_2017", genes=genes))
    assert crude["stratification"] is None
    assert any("NOT adjusted for tumour type" in w for w in crude["warnings"])

    adjusted = _ok(
        server.alteration_cooccurrence("msk_impact_2017", genes=genes, stratify_by="CANCER_TYPE")
    )
    assert adjusted["stratification"]["by"] == "CANCER_TYPE"
    assert adjusted["stratification"]["n_strata"] > 20
    assert all("crude" in pair and pair["test"] for pair in adjusted["pairs"])
    assert any(
        pair["tendency"] != pair["crude"]["tendency"]
        or (pair["q_value"] < 0.05) != (pair["crude"]["q_value"] < 0.05)
        for pair in adjusted["pairs"]
    ), "stratification should change at least one conclusion in a 58-cancer-type cohort"


def test_t7_genome_wide_scan_for_genes_enriched_in_tp53_wild_type():
    naive = _ok(server.alteration_cooccurrence("msk_impact_2017", alteration_types=["mutation"]))
    assert any("not a genome-wide search" in w for w in naive["warnings"])

    p = _ok(
        server.alteration_enrichment(
            "TP53: MUT", preference=TCGA, stratify_by="CANCER_TYPE", direction="B", max_results=20
        )
    )
    assert p["groups"]["A"]["n_samples"] == 3839 and p["groups"]["B"]["n_samples"] == 6604
    assert p["n_genes_tested"] > 15000
    assert p["excluded_genes"] == ["TP53"]
    assert p["stratification"]["by"] == "CANCER_TYPE"
    assert p["genes"] and all(g["enriched_in"] == "B" for g in p["genes"])
    assert "synthetic lethality" in p["notes"]


# --- oncoprint ------------------------------------------------------------------------------


def test_t8_merged_tracks_in_tcga_and_driver_filters_refused():
    q = (
        'SMARCA4; SMARCB1; ARID1A; ["All three" SMARCA4 SMARCB1 ARID1A]; '
        '["Truncating drivers" SMARCA4: TRUNC_DRIVER; SMARCB1: TRUNC_DRIVER; ARID1A: TRUNC_DRIVER]'
    )
    refused = server.oncoprint(preference=TCGA, oql=q, clinical_tracks=[])
    assert "DRIVER filters need driver annotations" in refused["error"]

    p = _ok(server.oncoprint(preference=TCGA, oql=q.replace("_DRIVER", ""), clinical_tracks=[]))
    stats = {g["gene"]: g for g in p["gene_stats"]}
    assert stats["All three"]["altered"] == 1505
    assert stats["All three"]["altered"] >= max(stats[g]["altered"] for g in ("SMARCA4", "ARID1A"))
    assert stats["Truncating drivers"]["altered"] == 700


def test_t9_exclusions_are_reported_not_asserted():
    crc = _ok(
        server.oncoprint(
            "coadread_tcga_pan_can_atlas_2018",
            oql="EGFR: MUT != T790M MUT != L858R",
            clinical_tracks=[],
        )
    )
    assert [x["events_removed"] for x in crc["exclusions"]] == [0, 0]
    assert any("removed nothing" in w for w in crc["warnings"])
    luad = _ok(
        server.oncoprint(
            "luad_tcga_pan_can_atlas_2018",
            oql="EGFR: MUT != T790M MUT != L858R",
            clinical_tracks=[],
        )
    )
    removed = {x["exclusion"]: x for x in luad["exclusions"]}
    assert removed["MUT != L858R"]["events_removed"] == 23


def test_t10_mutation_classes_can_be_collapsed():
    p = _ok(
        server.oncoprint(
            "luad_tcga_pan_can_atlas_2018",
            genes=["TP53", "KRAS", "EGFR"],
            clinical_tracks=[],
            mutation_classes="collapsed",
        )
    )
    assert {c["mut"] for row in p["cells"].values() for c in row.values() if c.get("mut")} == {
        "mutation"
    }


# --- mutation_diagram / nucleotide detail -----------------------------------------------------


def test_t11_egfr_mutations_in_the_tyrosine_kinase_domain_are_counted():
    p = _ok(
        server.mutation_diagram("luad_tcga_pan_can_atlas_2018", "EGFR", domain="tyrosine kinase")
    )
    region = p["region"]
    assert region["pfam_accession"] == "PF07714" and region["ranges"] == [[713, 965]]
    assert (region["n_samples_in_region"], region["n_samples_mutated_anywhere"]) == (60, 70)


def test_t12_codon_changes_behind_braf_v600e():
    p = _ok(
        server.nucleotide_variants(
            "BRAF", protein_change="V600E", study_id="skcm_tcga_pan_can_atlas_2018"
        )
    )
    assert p["codon_changes"] == [
        {"codon_change": "GTG/GAG", "n_samples": 158, "pct_of_samples": 100.0}
    ]
    silent = server.nucleotide_variants("EGFR", codon_change="GAG>GAA", preference=TCGA)
    assert "synonymous (silent)" in silent["error"]


def test_t13_lollipop_on_tcga_then_the_non_redundant_set():
    misuse = server.mutation_diagram(TCGA, "BAP1")
    assert "named study set" in misuse["error"]
    tcga = _ok(server.mutation_diagram(preference=TCGA, gene="BAP1"))
    assert tcga["scope"]["n_studies"] == 32 and tcga["n_samples_mutated"] == 241
    nr = _ok(server.mutation_diagram(preference="all_studies_non_redundant", gene="BAP1"))
    assert nr["scope"]["preference"] == "all_studies_non_redundant"
    assert nr["scope"]["n_studies"] > 200 and nr["n_samples_mutated"] > tcga["n_samples_mutated"]


# --- charts ------------------------------------------------------------------------------


def test_t14_vaf_histogram_of_tp53_missense_in_diploid_samples():
    p = _ok(
        server.mutation_allele_frequency("TP53: MISSENSE", preference=TCGA, copy_number="diploid")
    )
    assert p["kind"] == "histogram"
    assert p["counts"]["n_mutations"] == 953 and p["counts"]["n_samples"] == 838
    lines = {r["label"]: r["value"] for r in p["reference_lines"]}
    assert lines == {"mean": p["stats"]["mean"], "median": p["stats"]["median"]}
    assert "not whole-genome ploidy" in p["filters"]["copy_number"]["definition"]
    assert sum(b["count"] for b in p["bins"]) == 953
