"""Tests for survival_curve's custom groups, expression groups and stratified test.

These are the STRESS-test shapes that used to be inexpressible (co-mutation groups,
codon-range arms, expression quartiles) or silently confounded (a pan-cancer
comparison reported without adjusting for cancer type).
"""

import pytest
from _fakedb import FakeDB

from cbioportal_mcp import server

STUDY = "pan_study"


def _cohort(db: FakeDB, n: int = 12, cancer_types=("Lung", "Pancreas")):
    """n WES-profiled patients (one sample each) with OS data, alternating cancer types."""
    sids = []
    for i in range(1, n + 1):
        sid = db.add_sample(STUDY, f"s{i}", patient=f"p{i}", cancer_type=cancer_types[i % 2])
        db.add_survival(sid, months=float(10 * i), event=(i % 3 != 0))
        sids.append(sid)
    return sids


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    fake.add_study(STUDY)
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


def _groups(payload):
    return {g["name"]: g["n_patients"] for g in payload["groups"]}


def test_co_mutation_group_versus_single_mutation_group(db):
    s = _cohort(db)
    for i in (4, 5, 6):
        db.add_mutation(s[i - 1], "TP53", "R175H")
    for i in (4, 5, 6, 7, 8, 9):
        db.add_mutation(s[i - 1], "KRAS", "G12D")

    p = server.survival_curve(
        study_id=STUDY,
        groups=[
            {"name": "TP53+KRAS", "altered": ["TP53: MUT", "KRAS: MUT"]},
            {"name": "KRAS only", "altered": "KRAS: MUT", "unaltered": "TP53: MUT"},
        ],
    )

    assert "error" not in p, p.get("error")
    assert _groups(p) == {"TP53+KRAS": 3, "KRAS only": 3}
    assert p["grouping"]["type"] == "custom"
    assert p["grouping"]["groups"][1] == {
        "name": "KRAS only",
        "altered": ["KRAS: MUT"],
        "unaltered": ["TP53: MUT"],
        "n_patients": 3,
    }
    assert p["grouping"]["n_unassigned"] == 6
    assert p["stats"]["test"] == "log-rank" and p["stats"]["df"] == 1


def test_codon_range_arms_and_wild_type_are_disjoint(db):
    s = _cohort(db)
    db.add_mutation(s[0], "TP53", "P36fs", "Frame_Shift_Del")  # codon 36 -> arm 1
    db.add_mutation(s[1], "TP53", "R175H")  # arm 2
    db.add_mutation(s[2], "TP53", "R248Q")  # arm 2
    db.add_mutation(s[3], "TP53", "M1?", "Translation_Start_Site")  # codon 1 -> arm 1
    db.add_mutation(s[3], "TP53", "R273H")  # also arm 2 -> overlaps, excluded
    db.add_mutation(s[4], "TP53", "NA", "Splice_Region")  # no codon: in no arm

    p = server.survival_curve(
        study_id=STUDY,
        groups=[
            {"name": "codons 1-40", "altered": "TP53: MUT = (1-40)"},
            {"name": "other codons", "altered": "TP53: MUT = (41-)"},
            {"name": "wild-type", "unaltered": "TP53: MUT"},
        ],
    )

    assert _groups(p) == {"codons 1-40": 1, "other codons": 2, "wild-type": 7}
    assert p["grouping"]["n_overlapping_excluded"] == 1
    assert p["grouping"]["n_unassigned"] == 1  # the splice-only patient
    assert p["stats"]["df"] == 2
    assert any("matched more than one group" in w for w in p["warnings"])


def test_unaltered_requires_the_gene_to_be_profiled(db):
    s = _cohort(db, n=6)
    db.add_mutation(s[0], "TP53", "R175H")
    # A panel sample whose panel lacks TP53 cannot be called TP53 wild-type.
    off_panel = db.add_sample(STUDY, "s7", patient="p7", wes=False, panel_genes={"KRAS"})
    db.add_survival(off_panel, 70.0, True)

    p = server.survival_curve(
        study_id=STUDY,
        groups=[
            {"name": "mutant", "altered": "TP53: MUT"},
            {"name": "wild-type", "unaltered": "TP53: MUT"},
        ],
    )
    assert _groups(p) == {"mutant": 1, "wild-type": 5}
    assert p["grouping"]["n_unassigned"] == 1


@pytest.mark.parametrize(
    "groups, fragment",
    [
        ([{"altered": "TP53: MUT"}], "list of 2-4 group definitions"),
        ([{"altered": "TP53"}] * 5, "at most 4 groups"),
        ([{"name": "a"}, {"name": "b", "altered": "KRAS"}], "needs 'altered' and/or 'unaltered'"),
        ([{"altered": "TP53", "clinical": {}}, {"altered": "KRAS"}], "unsupported keys"),
        ([{"name": "x", "altered": "TP53"}, {"name": "x", "altered": "KRAS"}], "unique"),
        ([{"altered": "TP53 KRAS"}, {"altered": "BRAF"}], "describes 2 tracks"),
        ([{"altered": "TP53: EXP > 2"}, {"altered": "BRAF"}], "EXP is not supported"),
        ("TP53", "list of 2-4 group definitions"),
    ],
)
def test_invalid_group_definitions_are_rejected(db, groups, fragment):
    _cohort(db, n=4)
    p = server.survival_curve(study_id=STUDY, groups=groups)
    assert fragment in p["error"]


def test_only_one_grouping_at_a_time(db):
    _cohort(db, n=4)
    p = server.survival_curve(
        study_id=STUDY, group_by_gene="TP53", groups=[{"altered": "TP53"}, {"altered": "KRAS"}]
    )
    assert "one grouping at a time" in p["error"]


def test_group_by_gene_with_several_genes_points_to_groups(db):
    _cohort(db, n=4)
    p = server.survival_curve(study_id=STUDY, group_by_gene="TP53 KRAS")
    assert "use groups=[...]" in p["error"]


def test_driver_condition_is_refused_without_annotations(db):
    s = _cohort(db, n=4)
    db.add_mutation(s[0], "TP53", "R175H")
    p = server.survival_curve(
        study_id=STUDY,
        groups=[{"altered": "TP53: MUT_DRIVER"}, {"unaltered": "TP53: MUT"}],
    )
    assert "DRIVER filters need driver annotations" in p["error"]


# --- stratification and confounding -------------------------------------------


def test_stratified_logrank_is_the_headline_when_stratify_by_is_given(db):
    s = _cohort(db)
    for i in (1, 3, 5, 7, 9, 11):
        db.add_mutation(s[i - 1], "KRAS", "G12D")
    p = server.survival_curve(study_id=STUDY, group_by_gene="KRAS", stratify_by="CANCER_TYPE")

    assert p["stats"]["test"] == "stratified log-rank"
    assert p["stats_unstratified"]["test"] == "log-rank"
    assert p["stratification"]["by"] == "CANCER_TYPE"
    assert p["stratification"]["n_strata"] == 2
    assert not any("NOT adjusted" in w for w in p["warnings"])


def test_unstratified_comparison_across_cancer_types_warns(db):
    s = _cohort(db)
    for i in (1, 2, 3):
        db.add_mutation(s[i - 1], "KRAS", "G12D")
    p = server.survival_curve(study_id=STUDY, group_by_gene="KRAS", cohort={"SEX": ["x"]})
    assert "Cohort filter matched no" in p["error"]  # SEX absent: an honest error
    p = server.survival_curve(study_id=STUDY, group_by_gene="KRAS")
    assert p["stratification"] is None
    assert any("NOT adjusted for cancer type" in w for w in p["warnings"])


def test_patients_without_a_stratum_value_are_excluded_everywhere(db):
    s = _cohort(db, n=8)
    extra = db.add_sample(STUDY, "s9", patient="p9")  # no CANCER_TYPE
    db.add_survival(extra, 5.0, True)
    for sid in s[:4]:
        db.add_mutation(sid, "TP53", "R175H")
    p = server.survival_curve(study_id=STUDY, group_by_gene="TP53", stratify_by="CANCER_TYPE")
    assert sum(g["n_patients"] for g in p["groups"]) == 8
    assert p["cohort"]["n_patients"] == 8
    assert any("without a CANCER_TYPE value" in w for w in p["warnings"])


def test_stratify_by_study_uses_the_study_of_each_patient(db, monkeypatch):
    s = _cohort(db, n=6)
    other = [db.add_sample("second_study", f"t{i}", patient=f"q{i}") for i in range(1, 5)]
    for i, sid in enumerate(other):
        db.add_survival(sid, 15.0 * (i + 1), True)
    db.add_mutation(s[0], "TP53", "R175H")
    db.add_mutation(other[0], "TP53", "R175H")
    p = server.survival_curve(
        studies=[STUDY, "second_study"], group_by_gene="TP53", stratify_by="study"
    )
    assert p["stratification"]["by"] == "STUDY" and p["stratification"]["n_strata"] == 2
    assert p["scope"]["n_studies"] == 2 and p["study_id"] is None


# --- expression groups -----------------------------------------------------------


def test_expression_quartiles_within_the_study(db):
    s = _cohort(db)
    db.add_profile(STUDY, "rna_seq_v2_mrna_median_Zscores", "MRNA_EXPRESSION", "Z-SCORE")
    db.add_profile(STUDY, "rna_seq_v2_mrna", "MRNA_EXPRESSION", "CONTINUOUS")
    for i, sid in enumerate(s, start=1):
        db.add_expression(sid, "EGFR", float(i * 100), "rna_seq_v2_mrna")

    p = server.survival_curve(
        study_id=STUDY, group_by_expression={"gene": "EGFR", "split": "top_vs_bottom_quartile"}
    )

    assert "error" not in p, p.get("error")
    g = p["grouping"]
    assert g["type"] == "expression" and g["profiles"] == "rna_seq_v2_mrna"
    assert g["cutoffs"] == {"q1": 375.0, "q2": 650.0, "q3": 925.0}
    assert _groups(p) == {"top quartile": 3, "bottom quartile": 3}
    assert g["n_excluded_by_split"] == 6


def test_expression_profile_must_exist(db):
    _cohort(db, n=4)
    p = server.survival_curve(study_id=STUDY, group_by_expression={"gene": "EGFR"})
    assert "expression grouping is not possible" in p["error"]


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ("EGFR", "must be an object"),
        ({"gene": "EGFR", "split": "deciles"}, "split must be one of"),
        ({"gene": "EGFR", "cutoff": 2}, "unsupported keys"),
        ({"gene": "EGFR", "profile": "bad profile!"}, "Invalid expression profile"),
    ],
)
def test_expression_grouping_validation(db, spec, fragment):
    _cohort(db, n=4)
    p = server.survival_curve(study_id=STUDY, group_by_expression=spec)
    assert fragment in p["error"]
