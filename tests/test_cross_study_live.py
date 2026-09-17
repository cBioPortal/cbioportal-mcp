"""Live smoke tests for ``cross_study_alteration_frequency`` against a real ClickHouse.

Skipped unless ``CLICKHOUSE_HOST`` is set (export the connection variables the
server itself reads: CLICKHOUSE_HOST / PORT / USER / PASSWORD / SECURE / DATABASE).
Every other test in this suite fakes the database; these exercise the six real
queries end to end and pin the numbers that the LLM-prepped public clone gave on
2026-09-03. TCGA PanCancer Atlas is a frozen dataset, so its counts are asserted
exactly; the MSK registries are re-cloned and grow, so they get range checks.

Run: ``uv run --python 3.12 --extra dev pytest tests/test_cross_study_live.py -q``
"""

import os

import pytest

from cbioportal_mcp import server

pytestmark = pytest.mark.skipif(
    not os.getenv("CLICKHOUSE_HOST"), reason="live ClickHouse credentials not in the environment"
)

TCGA = "luad_tcga_pan_can_atlas_2018"
CHORD = "msk_chord_2024"


def _rows(payload):
    assert "error" not in payload, payload.get("error")
    return {r["study_id"]: r for r in payload["studies"]}


def test_tp53_luad_across_msk_chord_and_tcga():
    payload = server.cross_study_alteration_frequency(
        "TP53", studies=[CHORD, TCGA], cancer_type="LUAD"
    )
    rows = _rows(payload)
    tcga, chord = rows[TCGA], rows[CHORD]
    assert tcga["cohort_key"] == chord["cohort_key"] == "ONCOTREE_CODE"
    assert tcga["samples"] == {"cohort": 566, "profiled": 566, "altered": 295}
    assert tcga["frequency_pct"] == 52.1 and tcga["ci95"] == [48.0, 56.2]
    assert tcga["panels"] == ["WES"] and tcga["sample_types"] == {"Primary": 566}
    assert chord["samples"]["cohort"] > 5000 and 40 <= chord["frequency_pct"] <= 50
    assert chord["samples"]["profiled"] == chord["samples"]["cohort"]  # TP53 on every IMPACT panel
    assert tcga["status"] == chord["status"] == "included"
    assert payload["overlap"] == {
        "checked": True,
        "pairs": [],
        "excluded": [],
        "pooling_blocked": False,
    }
    assert payload["pooled"]["k"] == 2 and payload["heterogeneity"]["i2_pct"] > 75
    assert payload["difference_test"]["test"] == "chi_square_homogeneity"
    assert len(payload["provenance"]["queries"]) == 6


def test_cohort_predicate_reproduces_the_shipped_view():
    """gene_mutation_frequency_in_study's NSCLC row for MSK-CHORD, via the tool."""
    payload = server.cross_study_alteration_frequency(
        "TP53", studies=[CHORD], cohort={"CANCER_TYPE": ["Non-Small Cell Lung Cancer"]}
    )
    row = _rows(payload)[CHORD]
    view = server.run_select_query(
        f"SELECT altered_samples, profiled_samples FROM gene_mutation_frequency_in_study("
        f"study='{CHORD}', gene='TP53') WHERE cancer_type = 'Non-Small Cell Lung Cancer'"
    )
    assert row["samples"]["altered"] == view[0]["altered_samples"]
    assert row["samples"]["profiled"] == view[0]["profiled_samples"]


def test_tcga_releases_of_one_cohort_are_not_pooled():
    payload = server.cross_study_alteration_frequency(
        "TP53", studies=["luad_tcga", TCGA], cancer_type="LUAD"
    )
    rows = _rows(payload)
    assert rows["luad_tcga"]["status"] == "overlap" and rows["luad_tcga"]["overlaps_with"] == TCGA
    assert rows[TCGA]["status"] == "included"
    (pair,) = payload["overlap"]["pairs"]
    assert pair["studies"] == ["luad_tcga", TCGA] and pair["shared_patients"] >= 500
    assert payload["overlap"]["pooling_blocked"] is True and payload["pooled"] is None


def test_preference_resolution_reports_studies_without_the_cancer_type():
    payload = server.cross_study_alteration_frequency(
        "TP53", preference="pan_cancer_tcga", cancer_type="LUAD"
    )
    assert [r["study_id"] for r in payload["studies"]] == [TCGA]
    assert len(payload["studies_without_cohort"]) >= 30
    assert payload["pooled"] is None and payload["overlap"]["checked"] is False


def test_gene_absent_from_panels_is_not_covered_not_zero():
    payload = server.cross_study_alteration_frequency(
        "TTN", studies=[CHORD, TCGA], cancer_type="LUAD"
    )
    rows = _rows(payload)
    assert rows[CHORD]["status"] == "not_covered"
    assert rows[CHORD]["samples"]["profiled"] == 0 and rows[CHORD]["frequency_pct"] is None
    assert rows[TCGA]["status"] == "included" and rows[TCGA]["samples"]["profiled"] == 566
    assert any("not on any MUTATION_EXTENDED panel" in w for w in payload["warnings"])
