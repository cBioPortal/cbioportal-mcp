"""Tests for the cross-study alteration-frequency tool
(``cross_study_alteration_frequency``) and its data-shaping helpers.

The database is faked by monkeypatching ``run_select_query`` on the server
module, dispatching on the SQL text (same approach as ``test_cooccurrence.py``).
The tool issues six kinds of query -- study resolution, per-study attribute
availability, the grouped counts, the overlap probe, panels, and the
sample-type mix -- and the fake recognises each by a distinctive fragment.

The two traps this suite exists for: an answer for the wrong cohort (the
cancer-type filter must reach the denominator, and a study that cannot be
filtered must be reported, not silently answered whole), and a pooled number
that is a sum or that pools overlapping cohorts.
"""

import pytest

from cbioportal_mcp import server

TCGA = "luad_tcga_pan_can_atlas_2018"
CHORD = "msk_chord_2024"


def _study(sid, name=None, type_of_cancer_id="luad"):
    return {
        "cancer_study_identifier": sid,
        "name": name or sid,
        "type_of_cancer_id": type_of_cancer_id,
    }


def _attrs(sid, n_oncotree=100, n_detailed=100, n_cancer_types=1):
    return {
        "cancer_study_identifier": sid,
        "n_oncotree": n_oncotree,
        "n_detailed": n_detailed,
        "n_cancer_types": n_cancer_types,
    }


def _counts(sid, cohort, profiled, altered, patients=None):
    p_cohort, p_profiled, p_altered = patients or (cohort, profiled, altered)
    return {
        "study": sid,
        "cohort_samples": cohort,
        "profiled_samples": profiled,
        "altered_samples": altered,
        "cohort_patients": p_cohort,
        "profiled_patients": p_profiled,
        "altered_patients": p_altered,
    }


EXAMPLE_STUDIES = [
    _study(TCGA, "Lung Adenocarcinoma (TCGA, PanCancer Atlas)", "luad"),
    _study(CHORD, "MSK-CHORD (MSK, Nature 2024)", "mixed"),
]
EXAMPLE_ATTRS = [_attrs(TCGA, 566, 566, 1), _attrs(CHORD, 25040, 25040, 45)]
EXAMPLE_COUNTS = [_counts(TCGA, 566, 566, 295), _counts(CHORD, 5957, 5957, 2695)]
EXAMPLE_PANELS = [
    {"cancer_study_identifier": TCGA, "panels": ["WES"]},
    {"cancer_study_identifier": CHORD, "panels": ["IMPACT341", "IMPACT410", "IMPACT468"]},
]
EXAMPLE_SAMPLE_TYPES = [
    {"cancer_study_identifier": TCGA, "sample_type": "Primary", "n": 566},
    {"cancer_study_identifier": CHORD, "sample_type": "Primary", "n": 3715},
    {"cancer_study_identifier": CHORD, "sample_type": "Metastasis", "n": 2133},
]


def _fake_db(
    studies=EXAMPLE_STUDIES,
    attrs=EXAMPLE_ATTRS,
    counts=EXAMPLE_COUNTS,
    overlap=(),
    panels=EXAMPLE_PANELS,
    sample_types=EXAMPLE_SAMPLE_TYPES,
    preferences=None,
):
    """run_select_query stand-in; returns ``(fake_query, executed)``.

    Study resolution honours the SQL: a study is returned only if its id is
    quoted in the query or it belongs to a preference the query names.
    """
    preferences = preferences or {}
    executed: list[str] = []

    def fake_query(q):
        executed.append(q)
        if "SELECT cancer_study_identifier, name, type_of_cancer_id" in q:
            wanted = set()
            for pref, members in preferences.items():
                if f"preference_name = '{pref}'" in q:
                    wanted.update(members)
            rows = [
                s
                for s in studies
                if f"'{s['cancer_study_identifier']}'" in q
                or s["cancer_study_identifier"] in wanted
            ]
            return sorted(rows, key=lambda r: r["cancer_study_identifier"])
        if "AS n_oncotree" in q:
            return [a for a in attrs if f"'{a['cancer_study_identifier']}'" in q]
        if "AS cohort_samples" in q:
            return [c for c in counts if f"'{c['study']}'" in q]
        if "AS shared_ids" in q:
            return list(overlap)
        if "groupUniqArray(gene_panel_id)" in q:
            return [p for p in panels if f"'{p['cancer_study_identifier']}'" in q]
        if "AS sample_type" in q:
            return [s for s in sample_types if f"'{s['cancer_study_identifier']}'" in q]
        raise AssertionError(f"unexpected query: {q[:120]}")

    return fake_query, executed


def _run(monkeypatch, gene="TP53", **kwargs):
    fake = kwargs.pop("fake", None) or _fake_db()
    fake_query, executed = fake
    monkeypatch.setattr(server, "run_select_query", fake_query)
    payload = server.cross_study_alteration_frequency(gene, **kwargs)
    return payload, executed


def _sql(executed, fragment):
    hits = [q for q in executed if fragment in q]
    assert len(hits) == 1, f"expected one query containing {fragment!r}, got {len(hits)}"
    return hits[0]


# --- the worked example -------------------------------------------------------


def test_two_study_example_payload(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD")
    assert "error" not in payload
    assert payload["kind"] == "cross_study_frequency"
    assert payload["gene"] == "TP53" and payload["unit"] == "sample"
    assert payload["cancer_type"] == {
        "requested": ["LUAD"],
        "codes": ["LUAD"],
        "names": ["Lung Adenocarcinoma"],
        "include_subtypes": True,
    }

    rows = {r["study_id"]: r for r in payload["studies"]}
    assert list(rows) == [TCGA, CHORD]  # explicit order preserved
    tcga, chord = rows[TCGA], rows[CHORD]
    assert tcga["cohort_key"] == "ONCOTREE_CODE" and chord["cohort_key"] == "ONCOTREE_CODE"
    assert tcga["samples"] == {"cohort": 566, "profiled": 566, "altered": 295}
    assert tcga["frequency_pct"] == 52.1 and tcga["ci95"] == [48.0, 56.2]
    assert chord["frequency_pct"] == 45.2 and chord["ci95"] == [44.0, 46.5]
    assert tcga["status"] == chord["status"] == "included"
    assert tcga["panels"] == ["WES"] and chord["sample_types"] == {
        "Primary": 3715,
        "Metastasis": 2133,
    }
    assert (tcga["weight_pct"], chord["weight_pct"]) == (45.8, 54.2)

    pooled = payload["pooled"]
    assert pooled["method"] == "random_effects_dersimonian_laird" and pooled["k"] == 2
    assert pooled["frequency_pct"] == 48.4 and pooled["ci95"] == [41.7, 55.1]
    assert pooled["fixed_effect_pct"] == 45.8 and pooled["fixed_effect_ci95"] == [44.6, 47.1]
    assert (pooled["n_altered"], pooled["n_profiled"]) == (2990, 6523)
    het = payload["heterogeneity"]
    assert het["df"] == 1 and het["i2_pct"] == 89.8 and het["q"] == 9.805
    assert het["p_value"] == pytest.approx(0.00174, rel=1e-2)
    diff = payload["difference_test"]
    assert diff["test"] == "chi_square_homogeneity" and diff["df"] == 1
    assert diff["statistic"] == 9.853 and diff["p_value"] == pytest.approx(0.0017, abs=2e-4)

    assert payload["overlap"] == {
        "checked": True,
        "pairs": [],
        "excluded": [],
        "pooling_blocked": False,
    }
    assert payload["studies_without_cohort"] == []
    assert any("High between-study heterogeneity" in w for w in payload["warnings"])
    assert any("Counting unit: samples" in n for n in payload["notes"])
    assert any("not a sum of counts" in n for n in payload["notes"])
    # Six queries, recorded verbatim.
    assert len(executed) == 6
    assert payload["provenance"]["queries"] == executed


def test_the_high_heterogeneity_warning_names_the_designs(monkeypatch):
    payload, _ = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD")
    warning = next(w for w in payload["warnings"] if "heterogeneity" in w)
    assert "WES" in warning and "IMPACT341" in warning and "Metastasis 2133" in warning
    assert "per-study rates" in warning


def test_design_summary_is_capped():
    rows = [
        {"study_id": f"s{i}", "panels": ["WES"], "sample_types": {"Primary": 10, "Metastasis": 3}}
        for i in range(9)
    ]
    summary = server._cross_study_design_summary(rows, limit=6)
    assert summary.startswith("s0 (WES; Primary 10, Metastasis 3); s1 (WES")
    assert summary.endswith("; and 3 more") and "s6" not in summary
    assert server._cross_study_design_summary(rows[:2]) == (
        "s0 (WES; Primary 10, Metastasis 3); s1 (WES; Primary 10, Metastasis 3)"
    )
    assert server._cross_study_design_summary([{"study_id": "x"}]) == "x (design unknown)"


def test_patient_unit_drives_frequency_and_stats(monkeypatch):
    counts = [
        _counts(TCGA, 566, 566, 295),
        _counts(CHORD, 6254, 6254, 3094, patients=(5224, 5224, 2616)),
    ]
    payload, _ = _run(
        monkeypatch,
        studies=[TCGA, CHORD],
        cancer_type="LUAD",
        unit="patient",
        fake=_fake_db(counts=counts),
    )
    chord = payload["studies"][1]
    assert chord["samples"] == {"cohort": 6254, "profiled": 6254, "altered": 3094}
    assert chord["patients"] == {"cohort": 5224, "profiled": 5224, "altered": 2616}
    assert chord["frequency_pct"] == round(100 * 2616 / 5224, 1)
    assert payload["pooled"]["n_profiled"] == 566 + 5224
    assert any("Counting unit: patients" in n for n in payload["notes"])


# --- cohort resolution --------------------------------------------------------


def test_cohort_key_fallbacks_are_applied_and_disclosed(monkeypatch):
    studies = [
        _study("a_study", type_of_cancer_id="luad"),
        _study("b_study", type_of_cancer_id="luad"),
        _study("c_study", type_of_cancer_id="luad"),
        _study("d_study", type_of_cancer_id="brca"),
    ]
    attrs = [_attrs("a_study", 10, 10), _attrs("b_study", 0, 10)]  # c/d: no rows at all
    counts = [
        _counts("a_study", 10, 10, 5),
        _counts("b_study", 10, 10, 5),
        _counts("c_study", 10, 10, 5),
    ]
    payload, executed = _run(
        monkeypatch,
        studies=["a_study", "b_study", "c_study", "d_study"],
        cancer_type="LUAD",
        fake=_fake_db(studies=studies, attrs=attrs, counts=counts, panels=[], sample_types=[]),
    )
    keys = {r["study_id"]: r["cohort_key"] for r in payload["studies"]}
    assert keys == {
        "a_study": "ONCOTREE_CODE",
        "b_study": "CANCER_TYPE_DETAILED",
        "c_study": "STUDY_TYPE",
    }
    assert payload["studies_without_cohort"] == [
        {
            "study_id": "d_study",
            "reason": (
                "no per-sample ONCOTREE_CODE or CANCER_TYPE_DETAILED attribute, and the "
                "study's own cancer type 'brca' is not among the requested codes"
            ),
        }
    ]
    sql = _sql(executed, "AS cohort_samples")
    assert "attribute_name = 'ONCOTREE_CODE'" in sql and "'LUAD'" in sql
    assert "attribute_name = 'CANCER_TYPE_DETAILED'" in sql and "'LUNG ADENOCARCINOMA'" in sql
    assert "FROM sample_derived" in sql and "'c_study'" in sql
    assert "'d_study'" not in sql


def test_no_cancer_type_takes_whole_studies_and_flags_mixed_ones(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA, CHORD])
    assert payload["cancer_type"] is None
    assert {r["cohort_key"] for r in payload["studies"]} == {"ALL_SAMPLES"}
    sql = _sql(executed, "AS cohort_samples")
    assert "FROM sample_derived" in sql and "ONCOTREE_CODE" not in sql
    assert any(f"{CHORD} spans 45 cancer types" in w for w in payload["warnings"])
    assert not any(f"{TCGA} spans" in w for w in payload["warnings"])


def test_subtype_expansion(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="NSCLC")
    codes = payload["cancer_type"]["codes"]
    assert payload["cancer_type"]["requested"] == ["NSCLC"]
    assert {"NSCLC", "LUAD", "LUSC"} <= set(codes)
    assert "'LUSC'" in _sql(executed, "AS cohort_samples")

    payload, _ = _run(
        monkeypatch, studies=[TCGA, CHORD], cancer_type="NSCLC", include_subtypes=False
    )
    assert payload["cancer_type"]["codes"] == ["NSCLC"]


def test_cancer_type_accepts_names_and_lists(monkeypatch):
    payload, _ = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="Lung Adenocarcinoma")
    assert payload["cancer_type"]["codes"] == ["LUAD"]
    payload, _ = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type=["luad", "LUSC"])
    assert payload["cancer_type"]["requested"] == ["LUAD", "LUSC"]


def test_unknown_cancer_type_errors_with_suggestions(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA], cancer_type="lung adeno")
    assert "Unknown OncoTree code 'lung adeno'" in payload["error"]
    assert "search_oncotree" in payload["error"] and "LUAD" in payload["error"]
    assert payload["studies"] == [] and executed == []  # rejected before any query


def test_generic_cohort_predicate_narrows_every_query(monkeypatch):
    payload, executed = _run(
        monkeypatch,
        studies=[TCGA, CHORD],
        cancer_type="LUAD",
        cohort={"SAMPLE_TYPE": ["Primary"]},
    )
    assert payload["filter"] == {"SAMPLE_TYPE": ["Primary"]}
    counts_sql = _sql(executed, "AS cohort_samples")
    assert "attribute_name = 'SAMPLE_TYPE'" in counts_sql and "'PRIMARY'" in counts_sql
    # The same cohort CTE drives the overlap probe and the sample-type mix, so
    # numerator, denominator and disclosures cannot disagree about the cohort.
    cohort_cte = counts_sql.split("profiled AS (")[0]
    assert cohort_cte.startswith("\n        WITH cohort AS (")
    body = cohort_cte.split("WITH cohort AS (", 1)[1].rsplit(")", 1)[0]
    assert body in _sql(executed, "AS shared_ids")
    assert body in _sql(executed, "AS sample_type")


def test_cohort_predicate_that_matches_nothing_is_an_error_not_a_zero(monkeypatch):
    payload, _ = _run(
        monkeypatch,
        studies=[TCGA],
        cancer_type="LUAD",
        cohort={"SAMPLE_TYPE": ["Nope"]},
        fake=_fake_db(counts=[]),
    )
    assert "No samples matched the cohort" in payload["error"]
    assert "SAMPLE_TYPE" in payload["error"] and "provenance" in payload


# --- statuses and the pooling guard -------------------------------------------


def test_not_covered_is_explicit_and_blocks_pooling(monkeypatch):
    counts = [_counts(TCGA, 566, 566, 295), _counts(CHORD, 5957, 0, 0)]
    payload, _ = _run(
        monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD", fake=_fake_db(counts=counts)
    )
    chord = payload["studies"][1]
    assert chord["status"] == "not_covered"
    assert chord["frequency_pct"] is None and chord["ci95"] is None
    assert any("not on any MUTATION_EXTENDED panel" in w for w in payload["warnings"])
    assert payload["pooled"] is None and payload["difference_test"] is None
    assert any("not computed: 1 study eligible" in n for n in payload["notes"])


def test_below_min_profiled_is_shown_but_not_pooled(monkeypatch):
    studies = EXAMPLE_STUDIES + [_study("tiny_2020")]
    attrs = EXAMPLE_ATTRS + [_attrs("tiny_2020")]
    counts = EXAMPLE_COUNTS + [_counts("tiny_2020", 8, 5, 4)]
    payload, _ = _run(
        monkeypatch,
        studies=[TCGA, CHORD, "tiny_2020"],
        cancer_type="LUAD",
        fake=_fake_db(studies=studies, attrs=attrs, counts=counts),
    )
    tiny = payload["studies"][2]
    assert tiny["status"] == "below_min_profiled"
    assert tiny["frequency_pct"] == 80.0 and tiny["ci95"] is not None
    assert tiny["weight_pct"] is None
    assert payload["pooled"]["k"] == 2 and payload["pooled"]["studies"] == [TCGA, CHORD]
    assert any("tiny_2020" in n and "fewer than 10" in n for n in payload["notes"])

    payload, _ = _run(
        monkeypatch,
        studies=[TCGA, CHORD, "tiny_2020"],
        cancer_type="LUAD",
        min_profiled=5,
        fake=_fake_db(studies=studies, attrs=attrs, counts=counts),
    )
    assert payload["studies"][2]["status"] == "included" and payload["pooled"]["k"] == 3


def test_overlap_excludes_the_smaller_study_and_reports_the_pair(monkeypatch):
    legacy = "luad_tcga"
    studies = EXAMPLE_STUDIES + [_study(legacy, "Lung Adenocarcinoma (TCGA, Firehose Legacy)")]
    attrs = EXAMPLE_ATTRS + [_attrs(legacy, 500, 500)]
    counts = EXAMPLE_COUNTS + [_counts(legacy, 500, 500, 260)]
    overlap = [
        {"grain": "patient", "studies": [legacy, TCGA], "shared_ids": 564},
        {"grain": "sample", "studies": [legacy, TCGA], "shared_ids": 560},
    ]
    payload, _ = _run(
        monkeypatch,
        studies=[TCGA, legacy, CHORD],
        cancer_type="LUAD",
        fake=_fake_db(studies=studies, attrs=attrs, counts=counts, overlap=overlap),
    )
    rows = {r["study_id"]: r for r in payload["studies"]}
    assert rows[legacy]["status"] == "overlap" and rows[legacy]["overlaps_with"] == TCGA
    assert rows[legacy]["frequency_pct"] == 52.0  # the row itself is untouched
    assert rows[TCGA]["status"] == rows[CHORD]["status"] == "included"
    assert payload["overlap"]["pairs"] == [
        {"studies": [legacy, TCGA], "shared_patients": 564, "shared_samples": 560}
    ]
    assert payload["overlap"]["excluded"] == [legacy]
    assert payload["overlap"]["pooling_blocked"] is False
    assert payload["pooled"]["k"] == 2 and payload["pooled"]["studies"] == [TCGA, CHORD]
    assert any(
        "share 564 patients" in w and "excluded from pooling" in w for w in payload["warnings"]
    )


def test_overlap_between_the_only_two_studies_blocks_pooling(monkeypatch):
    overlap = [{"grain": "patient", "studies": [CHORD, TCGA], "shared_ids": 3}]
    payload, _ = _run(
        monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD", fake=_fake_db(overlap=overlap)
    )
    assert payload["pooled"] is None and payload["difference_test"] is None
    assert payload["overlap"]["excluded"] == [TCGA]  # smaller profiled cohort
    assert payload["overlap"]["pooling_blocked"] is True


def test_overlap_probe_skipped_for_a_single_study(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA], cancer_type="LUAD")
    assert payload["overlap"] == {
        "checked": False,
        "pairs": [],
        "excluded": [],
        "pooling_blocked": False,
    }
    assert not any("AS shared_ids" in q for q in executed)
    assert len(executed) == 5


def test_pool_false_keeps_rows_heterogeneity_and_test(monkeypatch):
    payload, _ = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD", pool=False)
    assert payload["pooled"] is None
    assert payload["heterogeneity"]["i2_pct"] == 89.8
    assert payload["difference_test"]["p_value"] == pytest.approx(0.0017, abs=2e-4)
    assert all(r["weight_pct"] is None for r in payload["studies"])
    assert any("pool=False" in n for n in payload["notes"])


# --- study resolution ---------------------------------------------------------


def test_unknown_study_id_is_an_error(monkeypatch):
    payload, executed = _run(monkeypatch, studies=[TCGA, "nope_2020"], cancer_type="LUAD")
    assert "Unknown study id(s): nope_2020" in payload["error"]
    assert payload["studies"] == [] and payload["kind"] == "cross_study_frequency"
    assert payload["provenance"]["queries"] == executed and len(executed) == 1


def test_preference_members_are_unioned_with_explicit_studies(monkeypatch):
    fake = _fake_db(preferences={"pan_cancer_tcga": [TCGA]})
    payload, executed = _run(
        monkeypatch, studies=[CHORD], preference="pan_cancer_tcga", cancer_type="LUAD", fake=fake
    )
    resolve_sql = executed[0]
    assert f"'{CHORD}'" in resolve_sql and "preference_name = 'pan_cancer_tcga'" in resolve_sql
    assert payload["preference"] == "pan_cancer_tcga"
    assert [r["study_id"] for r in payload["studies"]] == [CHORD, TCGA]  # explicit first


def test_empty_preference_is_an_error(monkeypatch):
    payload, _ = _run(monkeypatch, preference="no_such_set", fake=_fake_db(studies=[]))
    assert "Preference 'no_such_set' resolved to no studies" in payload["error"]


def test_studies_accepts_a_comma_separated_string(monkeypatch):
    payload, _ = _run(monkeypatch, studies=f"{TCGA}, {CHORD}", cancer_type="LUAD")
    assert [r["study_id"] for r in payload["studies"]] == [TCGA, CHORD]


# --- alteration types and SQL shape ------------------------------------------


def test_amplification_switches_numerator_and_denominator(monkeypatch):
    payload, executed = _run(
        monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD", alteration="amplification"
    )
    sql = _sql(executed, "AS cohort_samples")
    assert "cna_alteration = 2" in sql
    assert "stgp.alteration_type = 'COPY_NUMBER_ALTERATION'" in sql
    assert "mutation_panel_gene_coverage" not in sql
    panels_sql = _sql(executed, "groupUniqArray(gene_panel_id)")
    assert "alteration_type = 'COPY_NUMBER_ALTERATION'" in panels_sql
    assert any("COPY_NUMBER_ALTERATION" in n for n in payload["notes"])


def test_counts_sql_uses_exact_guarded_distincts(monkeypatch):
    _, executed = _run(monkeypatch, studies=[TCGA, CHORD], cancer_type="LUAD")
    sql = _sql(executed, "AS cohort_samples")
    assert "uniqExactIf(c.sample_unique_id, p.sample_unique_id != '')" in sql
    assert (
        "uniqExactIf(c.sample_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '')"
        in sql
    )
    assert "COUNT(DISTINCT" not in sql and "uniq(" not in sql
    assert "mutation_status != 'UNCALLED'" in sql and "off_panel = 0" in sql
    assert "FROM mutation_wes_coverage" in sql and "FROM mutation_panel_gene_coverage" in sql


# --- error contract and registration -----------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"gene": "TP53;DROP", "studies": [TCGA]}, "Invalid gene symbol"),
        ({"studies": [TCGA], "unit": "tumour"}, "unit must be one of"),
        ({"studies": [TCGA], "alteration": "fusion"}, "Invalid alteration_type"),
        ({"studies": [TCGA], "min_profiled": 0}, "min_profiled must be at least 1"),
        ({"studies": ["bad id!"]}, "Invalid study_id"),
        ({"preference": "pan-cancer"}, "Invalid preference name"),
        ({}, "Name the studies to compare"),
        ({"studies": [TCGA], "cohort": {"A": ["x"], "B": ["y"]}}, "exactly one attribute"),
    ],
)
def test_validation_errors_keep_the_contract_shape(monkeypatch, kwargs, fragment):
    gene = kwargs.pop("gene", "TP53")
    payload, executed = _run(monkeypatch, gene=gene, **kwargs)
    assert fragment in payload["error"]
    assert payload["kind"] == "cross_study_frequency" and payload["studies"] == []
    assert executed == [] and "provenance" not in payload


async def test_tool_is_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "cross_study_alteration_frequency" in names
