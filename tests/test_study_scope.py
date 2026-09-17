"""Tests for multi-study scopes (studies / preference) shared by the data apps.

The failure these guard against was silent: passing a study-set name such as
'pan_cancer_tcga' as ``study_id`` made every query match nothing, and the lollipop
answered "No mutations found for BAP1" -- a zero that looked real.
"""

import pytest
from _fakedb import FakeDB

from cbioportal_mcp import server


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    for study in ("luad_tcga", "paad_tcga", "brca_tcga"):
        fake.add_study(study)
    fake.add_preference("pan_cancer_tcga", ["luad_tcga", "paad_tcga"], "TCGA PanCancer Atlas 2018")
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


def test_single_study_scope_keeps_the_legacy_sql():
    scope = server._scope_of("brca_tcga")
    assert scope.single and scope.label == "brca_tcga"
    assert scope.sql() == "cancer_study_identifier = 'brca_tcga'"
    assert scope.sql("c.cancer_study_identifier") == "c.cancer_study_identifier = 'brca_tcga'"


def test_preference_scope_resolves_and_filters_through_the_preference(db):
    scope = server._resolve_scope(None, None, "pan_cancer_tcga")
    assert scope.study_ids == ("luad_tcga", "paad_tcga")
    assert scope.label == "pan_cancer_tcga (2 studies)"
    assert "preference_name = 'pan_cancer_tcga'" in scope.sql()
    assert scope.describe()["preference_notes"] == "TCGA PanCancer Atlas 2018"


def test_explicit_studies_scope(db):
    scope = server._resolve_scope(None, ["brca_tcga", "luad_tcga"])
    assert scope.study_ids == ("brca_tcga", "luad_tcga")
    assert scope.sql() == "cancer_study_identifier IN ('brca_tcga', 'luad_tcga')"
    assert scope.label == "2 studies"


def test_one_explicit_study_is_a_single_scope(db):
    scope = server._resolve_scope(None, ["brca_tcga"])
    assert scope.single and scope.verified


@pytest.mark.parametrize(
    "args, fragment",
    [
        (("luad_tcga", ["paad_tcga"], None), "not both"),
        ((None, None, None), "Name the cohort"),
        ((None, ["nope_study"], None), "'nope_study' is not a study in this deployment"),
        ((None, ["pan_cancer_tcga"], None), "Pass preference='pan_cancer_tcga'"),
        ((None, None, "bogus_set"), "Unknown preference 'bogus_set'"),
        ((None, None, "bad-name"), "Invalid preference name"),
        (("bad id!", None, None), "Invalid study_id"),
    ],
)
def test_scope_errors(db, args, fragment):
    with pytest.raises(ValueError, match=fragment.replace("(", r"\(").replace(")", r"\)")):
        server._resolve_scope(*args)


def test_study_set_name_passed_as_study_id_is_diagnosed_not_reported_as_zero(db):
    out = server.mutation_diagram(study_id="pan_cancer_tcga", gene="BAP1")
    assert "is not a study id: it is a named study set (2 studies" in out["error"]
    assert "preference='pan_cancer_tcga'" in out["error"]


def test_real_study_without_data_keeps_the_plain_message(db):
    out = server.mutation_diagram(study_id="brca_tcga", gene="BAP1")
    assert "No mutations found for BAP1" in out["error"]


def test_overlap_warning_names_studies_sharing_patient_ids(db):
    db.add_sample("luad_tcga", "s1", patient_stable_id="P-1")
    db.add_sample("paad_tcga", "s9", patient_stable_id="P-1")
    scope = server._resolve_scope(None, ["luad_tcga", "paad_tcga"])
    warning = server._scope_overlap_warning(scope)
    assert warning.startswith("1 patient id(s) occur in more than one study")
    assert "luad_tcga / paad_tcga" in warning
    assert server._scope_overlap_warning(server._scope_of("luad_tcga")) is None


def test_cohort_block_describes_a_multi_study_scope(db):
    db.add_sample("luad_tcga", "s1", cancer_type="Lung")
    db.add_sample("paad_tcga", "s2", cancer_type="Pancreas")
    scope = server._resolve_scope(None, None, "pan_cancer_tcga")
    warnings: list[str] = []
    block = server._cohort_block(scope, None, 2, None, warnings)
    assert block["study_id"] is None
    assert block["studies"]["n_studies"] == 2
    assert block["n_cancer_types"] == 2
    assert warnings == [
        "No cohort filter applied: spans 2 samples across 2 cancer types in 2 studies."
    ]


@pytest.mark.parametrize(
    "tool, kwargs",
    [
        (server.survival_curve, {}),
        (server.oncoprint, {"genes": ["TP53"]}),
        (server.mutation_diagram, {"gene": "TP53"}),
        (server.alteration_cooccurrence, {"genes": ["TP53", "KRAS"]}),
    ],
    ids=["survival", "oncoprint", "lollipop", "cooccurrence"],
)
def test_every_data_app_accepts_studies_and_preference(db, tool, kwargs):
    import inspect

    params = inspect.signature(tool).parameters
    assert params["study_id"].default is None
    assert params["studies"].default is None and params["preference"].default is None
    out = tool(preference="no_such_set", **kwargs)
    assert "Unknown preference 'no_such_set'" in out["error"]
