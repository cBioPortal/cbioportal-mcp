"""Tests for OQL tracks in the OncoPrint and co-occurrence apps, and stratified co-occurrence.

Covers the STRESS shapes: merged tracks, exclusions whose effect is reported rather
than asserted, DRIVER filters refused where no annotation exists, a collapsed mutation
legend, pathway-level exclusivity, and co-occurrence adjusted for tumour type.
"""

import pytest
from _fakedb import FakeDB

from cbioportal_mcp import server

STUDY = "study_x"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    fake.add_study(STUDY)
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


def _samples(db, n, **kwargs):
    return [db.add_sample(STUDY, f"s{i}", **kwargs) for i in range(1, n + 1)]


# --- OncoPrint ------------------------------------------------------------------


def test_merged_and_filtered_tracks_keep_user_order(db):
    s = _samples(db, 10)
    db.add_mutation(s[0], "SMARCA4", "R1157W")
    db.add_mutation(s[1], "SMARCA4", "Q729*", "Nonsense_Mutation")
    db.add_mutation(s[2], "ARID1A", "Q403fs", "Frame_Shift_Del")
    db.add_mutation(s[3], "ARID1A", "G2087R")
    db.add_cna(s[4], "SMARCB1", -2)

    q = (
        'SMARCA4; ARID1A; ["All three" SMARCA4 SMARCB1 ARID1A]; '
        '["Truncating" SMARCA4: TRUNC; SMARCB1: TRUNC; ARID1A: TRUNC]'
    )
    p = server.oncoprint(study_id=STUDY, oql=q, clinical_tracks=[])

    assert "error" not in p, p.get("error")
    assert p["genes"] == ["SMARCA4", "ARID1A", "All three", "Truncating"]
    stats = {g["gene"]: (g["altered"], g["profiled"]) for g in p["gene_stats"]}
    assert stats == {
        "SMARCA4": (2, 10),
        "ARID1A": (2, 10),
        "All three": (5, 10),
        "Truncating": (2, 10),
    }
    assert p["cells"]["Truncating"][s[1]]["mut"] == "truncating"
    assert p["query"].splitlines()[2] == '["All three" SMARCA4 SMARCB1 ARID1A]'
    assert [t["merged"] for t in p["tracks"]] == [False, False, True, True]
    assert p["exclusions"] == []


def test_exclusions_report_what_they_removed(db):
    s = _samples(db, 6)
    db.add_mutation(s[0], "EGFR", "T790M")
    db.add_mutation(s[1], "EGFR", "L858R")
    db.add_mutation(s[1], "EGFR", "G719S")  # still altered through G719S
    db.add_mutation(s[2], "EGFR", "E746_A750del", "In_Frame_Del")

    p = server.oncoprint(study_id=STUDY, oql="EGFR: MUT != T790M MUT != L858R", clinical_tracks=[])

    label = "EGFR: MUT != T790M MUT != L858R"
    assert p["gene_stats"][0]["gene"] == label and p["gene_stats"][0]["altered"] == 2
    assert set(p["cells"][label]) == {s[1], s[2]}
    assert p["exclusions"] == [
        {
            "track": label,
            "gene": "EGFR",
            "exclusion": "MUT != T790M",
            "events_removed": 1,
            "samples_removed": 1,
        },
        {
            "track": label,
            "gene": "EGFR",
            "exclusion": "MUT != L858R",
            "events_removed": 1,
            "samples_removed": 0,
        },
    ]


def test_exclusion_matching_nothing_is_called_out(db):
    s = _samples(db, 3)
    db.add_mutation(s[0], "EGFR", "G719S")
    p = server.oncoprint(study_id=STUDY, oql="EGFR: MUT != T790M", clinical_tracks=[])
    assert any("matched no event in this cohort" in w for w in p["warnings"])


def test_driver_filter_refused_without_annotations_and_allowed_with_them(db):
    s = _samples(db, 4)
    db.add_mutation(s[0], "TP53", "R175H")
    p = server.oncoprint(study_id=STUDY, oql="TP53: MUT_DRIVER", clinical_tracks=[])
    assert "DRIVER filters need driver annotations" in p["error"]

    db.add_mutation(s[1], "TP53", "R248Q", driver="Putative_Driver")
    p = server.oncoprint(study_id=STUDY, oql="TP53: MUT_DRIVER", clinical_tracks=[])
    assert "error" not in p
    assert set(p["cells"]["TP53: MUT_DRIVER"]) == {s[1]}


def test_collapsed_mutation_classes(db):
    s = _samples(db, 3)
    db.add_mutation(s[0], "TP53", "R175H")
    db.add_mutation(s[1], "TP53", "R213*", "Nonsense_Mutation")
    for kwargs in ({"genes": ["TP53"]}, {"oql": "TP53: MUT"}):
        p = server.oncoprint(
            study_id=STUDY, clinical_tracks=[], mutation_classes="collapsed", **kwargs
        )
        classes = {c["mut"] for row in p["cells"].values() for c in row.values()}
        assert classes == {"mutation"}
        assert p["mutation_classes"] == "collapsed"
    bad = server.oncoprint(study_id=STUDY, genes=["TP53"], mutation_classes="simple")
    assert "mutation_classes must be one of detailed, collapsed" in bad["error"]


def test_genes_and_oql_are_exclusive(db):
    _samples(db, 2)
    p = server.oncoprint(study_id=STUDY, genes=["TP53"], oql="TP53: MUT")
    assert "either genes/alteration_types or oql" in p["error"]


def test_merged_track_profiled_when_any_gene_is(db):
    wes = _samples(db, 2)
    panel = db.add_sample(STUDY, "p1", wes=False, panel_genes={"BRCA1"})
    db.add_mutation(wes[0], "BRCA2", "K3326*", "Nonsense_Mutation")
    p = server.oncoprint(study_id=STUDY, oql="[BRCA1 BRCA2]", clinical_tracks=[])
    stats = p["gene_stats"][0]
    assert stats["profiled"] == 3 and stats["altered"] == 1
    assert panel not in p["not_profiled"].get("BRCA1 / BRCA2", [])


# --- co-occurrence ------------------------------------------------------------------


def test_pathway_tracks_use_complete_case_profiling(db):
    s = _samples(db, 8)
    partial = db.add_sample(STUDY, "panel1", wes=False, panel_genes={"CDKN2A", "BRCA1"})
    for i in (0, 1, 2):
        db.add_mutation(s[i], "CDKN2A", "R80*", "Nonsense_Mutation")
    for i in (3, 4):
        db.add_mutation(s[i], "BRCA2", "K3326*", "Nonsense_Mutation")
    db.add_mutation(partial, "CDKN2A", "R80*", "Nonsense_Mutation")

    p = server.alteration_cooccurrence(
        study_id=STUDY,
        tracks=['["Cell cycle" CDKN2A CDK4]', '["HRR" BRCA1 BRCA2]'],
    )

    assert "error" not in p, p.get("error")
    assert p["genes"] == ["Cell cycle", "HRR"]
    by = {g["gene"]: g for g in p["gene_stats"]}
    # The panel sample lacks CDK4 and BRCA2, so neither pathway is tested on it.
    assert by["Cell cycle"]["profiled"] == 8 and by["Cell cycle"]["altered"] == 3
    pair = p["pairs"][0]
    assert (pair["n_both"], pair["n_a_only"], pair["n_b_only"], pair["n_neither"]) == (0, 3, 2, 3)
    assert pair["tendency"] == "Mutual exclusivity"
    assert "tested only on samples profiled for all of its genes" in p["notes"]


def _simpson(db):
    """Two cancer types; within each, TP53 and APC are independent, but both are
    common in colorectal and rare in lung, so the pooled table shows co-occurrence."""
    samples = {}
    for ct, n, n_tp53, n_apc, n_both in (("Colorectal", 100, 50, 50, 25), ("Lung", 100, 5, 5, 0)):
        ids = [db.add_sample(STUDY, f"{ct}{i}", cancer_type=ct) for i in range(n)]
        both = ids[:n_both]
        tp53_only = ids[n_both:n_tp53]
        apc_only = ids[n_tp53 : n_tp53 + (n_apc - n_both)]
        for sid in both + tp53_only:
            db.add_mutation(sid, "TP53", "R175H")
        for sid in both + apc_only:
            db.add_mutation(sid, "APC", "R1450*", "Nonsense_Mutation")
        samples[ct] = ids
    return samples


def test_stratified_cooccurrence_removes_tissue_confounding(db):
    _simpson(db)
    crude = server.alteration_cooccurrence(study_id=STUDY, genes=["TP53", "APC"])
    assert crude["pairs"][0]["tendency"] == "Co-occurrence"
    assert crude["pairs"][0]["p_value"] < 0.01
    assert crude["stratification"] is None
    assert any("NOT adjusted for tumour type" in w for w in crude["warnings"])

    adjusted = server.alteration_cooccurrence(
        study_id=STUDY, genes=["TP53", "APC"], stratify_by="CANCER_TYPE"
    )
    pair = adjusted["pairs"][0]
    assert pair["p_value"] > 0.2
    assert pair["crude"]["p_value"] == pytest.approx(crude["pairs"][0]["p_value"])
    assert pair["test"] in ("exact_conditional", "cmh_chi_square")
    assert pair["n_strata_informative"] == 2
    strat = adjusted["stratification"]
    assert strat["by"] == "CANCER_TYPE" and strat["n_strata"] == 2
    assert {s["name"] for s in strat["strata"]} == {"Colorectal", "Lung"}
    assert not any("NOT adjusted" in w for w in adjusted["warnings"])


def test_auto_selected_genes_say_they_are_not_a_genome_wide_search(db):
    s = _samples(db, 4)
    db.add_mutation(s[0], "TP53", "R175H")
    db.add_mutation(s[1], "KRAS", "G12D")
    p = server.alteration_cooccurrence(study_id=STUDY)
    assert any(
        "not a genome-wide search" in w and "alteration_enrichment" in w for w in p["warnings"]
    )


def test_tracks_and_genes_are_exclusive_and_bounded(db):
    _samples(db, 2)
    p = server.alteration_cooccurrence(study_id=STUDY, genes=["TP53", "KRAS"], tracks=["TP53"])
    assert "either genes/alteration_types or tracks" in p["error"]
    p = server.alteration_cooccurrence(study_id=STUDY, tracks=[f"G{i}" for i in range(13)])
    assert "tests at most 12" in p["error"]
