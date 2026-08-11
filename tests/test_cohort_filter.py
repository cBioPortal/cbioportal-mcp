"""Tests for the shared cohort filter and the cohort disclosure block.

Covers the two traps that produce plausible-but-wrong output if missed:

1. the cohort must narrow the *profiled denominator* as well as the alterations,
   or every frequency in the payload is computed against the wrong universe;
2. clinical attributes are sample-grain while survival is patient-grain, so a
   multi-primary patient whose samples disagree has no single cohort membership
   and must be excluded and counted, not silently dropped or double-counted.

The database is faked by monkeypatching ``run_select_query`` on the server
module, dispatching on the SQL text (same approach as ``test_cooccurrence.py``).
"""

import pytest

from cbioportal_mcp import server

# --- fake DB -----------------------------------------------------------------


def _ev(sample, gene, variant_type="mutation", mutation_type="Missense_Mutation"):
    """One genomic_event_derived row."""
    return {
        "sample_unique_id": sample,
        "patient_unique_id": "p_" + sample,
        "hugo_gene_symbol": gene,
        "variant_type": variant_type,
        "mutation_type": mutation_type,
    }


def _fake_db(
    events=None,
    wes_samples=(),
    clinical=None,
    survival=None,
    cancer_type_spread=None,
):
    """run_select_query stand-in dispatching on the query text.

    ``clinical`` is a list of (sample_unique_id, patient_unique_id,
    attribute_name, attribute_value) tuples backing every clinical_data_derived
    query the cohort helpers issue.
    """
    clinical = clinical or []
    survival = survival or []

    def fake_query(q):
        if "FROM mutation_wes_coverage" in q:
            return [{"sample_unique_id": s} for s in wes_samples]
        if "FROM mutation_panel_gene_coverage" in q:
            return []
        if "FROM genomic_event_derived" in q:
            if "COUNT(DISTINCT" in q:  # top-genes resolver
                return []
            return events or []
        if "FROM clinical_data_derived" in q:
            # The unfiltered-spread probe.
            if "n_cancer_types" in q:
                n_samples, n_types = cancer_type_spread or (0, 0)
                return [{"n_samples": n_samples, "n_cancer_types": n_types}]
            # The survival observations query.
            if "_MONTHS" in q:
                return survival
            # Sample-grain cohort lookup: SELECT DISTINCT sample_unique_id.
            if "DISTINCT sample_unique_id" in q:
                attr = _attr_of(q)
                wanted = _in_values_of(q)
                return [
                    {"sample_unique_id": sid}
                    for sid, _pid, name, value in clinical
                    if name == attr and value.upper() in wanted
                ]
            # Patient-grain lookup used by _clinical_patient_values:
            # SELECT DISTINCT patient_unique_id, attribute_value.
            if "DISTINCT patient_unique_id" in q:
                attr = _attr_of(q)
                return [
                    {"patient_unique_id": pid, "attribute_value": value}
                    for _sid, pid, name, value in clinical
                    if name == attr
                ]
        return []

    return fake_query


def _attr_of(query):
    """Pull the attribute_name literal out of a generated query."""
    marker = "attribute_name = '"
    start = query.index(marker) + len(marker)
    return query[start : query.index("'", start)]


def _in_values_of(query):
    """Pull the upper-cased IN (...) list out of a generated cohort query."""
    marker = "upper(attribute_value) IN ("
    start = query.index(marker) + len(marker)
    body = query[start : query.index(")", start)]
    return {part.strip().strip("'") for part in body.split(",")}


def _setup(monkeypatch, **kwargs):
    monkeypatch.setattr(server, "run_select_query", _fake_db(**kwargs))


BREAST = "Breast Cancer"
LUNG = "Lung Adenocarcinoma"


# --- predicate validation ----------------------------------------------------


def test_parse_cohort_accepts_single_attribute():
    assert server._parse_cohort({"CANCER_TYPE": [BREAST]}) == ("CANCER_TYPE", [BREAST])


def test_parse_cohort_accepts_bare_string_value():
    # Models routinely pass a scalar instead of a list.
    scalar_cohort: dict = {"CANCER_TYPE": BREAST}
    assert server._parse_cohort(scalar_cohort) == ("CANCER_TYPE", [BREAST])


def test_parse_cohort_rejects_invalid_attribute_name():
    # Goes through _validate_attribute_name -- the SQL is string-interpolated,
    # so this is the injection boundary.
    with pytest.raises(ValueError, match="Invalid attribute name"):
        server._parse_cohort({"CANCER_TYPE'; DROP TABLE x--": [BREAST]})


def test_parse_cohort_rejects_compound_predicate():
    with pytest.raises(ValueError, match="exactly one attribute"):
        server._parse_cohort({"CANCER_TYPE": [BREAST], "SEX": ["Female"]})


def test_parse_cohort_rejects_empty_values():
    with pytest.raises(ValueError, match="no non-empty values"):
        server._parse_cohort({"CANCER_TYPE": ["", "  "]})


def test_escape_sql_string_preserves_like_wildcards():
    # _sanitize_search_term would escape % and _ for LIKE; in an = / IN
    # comparison they are literal characters and must survive untouched.
    assert server._escape_sql_string("MSI_HIGH") == "MSI_HIGH"
    assert server._escape_sql_string("50%") == "50%"
    assert server._escape_sql_string("O'Brien") == "O''Brien"
    assert server._escape_sql_string("back\\slash") == "back\\\\slash"


def test_cohort_sample_ids_is_none_without_filter(monkeypatch):
    # None (no filter) and set() (filter matched nothing) must stay distinct.
    _setup(monkeypatch)
    assert server._cohort_sample_ids("study", None) is None


def test_cohort_matching_is_case_insensitive(monkeypatch):
    clinical = [("s1", "p_s1", "CANCER_TYPE", BREAST)]
    _setup(monkeypatch, clinical=clinical)
    assert server._cohort_sample_ids("study", {"CANCER_TYPE": ["BREAST CANCER"]}) == {"s1"}


# --- trap 1: the denominator must move with the numerator --------------------


def test_cohort_filters_numerator_and_denominator_together(monkeypatch):
    """s1-s4 breast, s5-s10 lung; TP53 altered in s1, s2 and s5.

    Within breast the rate is 2/4 = 50%. If only the alterations were filtered
    while the denominator stayed study-wide it would read 2/10 = 20%.
    """
    events = [_ev(s, "TP53") for s in ["s1", "s2", "s5"]]
    samples = [f"s{i}" for i in range(1, 11)]
    clinical = [
        (s, "p_" + s, "CANCER_TYPE", BREAST if s in {"s1", "s2", "s3", "s4"} else LUNG)
        for s in samples
    ]
    _setup(monkeypatch, events=events, wes_samples=samples, clinical=clinical)

    p = server._build_oncoprint_payload(
        "study", ["TP53"], ["mutation"], [], None, cohort={"CANCER_TYPE": [BREAST]}
    )

    assert "error" not in p
    stats = {g["gene"]: g for g in p["gene_stats"]}["TP53"]
    assert stats["profiled"] == 4, "denominator must be cohort-restricted"
    assert stats["altered"] == 2
    assert stats["freq_pct"] == 50.0
    assert p["cohort"]["n_samples"] == 4
    assert p["cohort"]["unfiltered"] is False
    assert p["cohort"]["filter"] == {"CANCER_TYPE": [BREAST]}


def test_cooccurrence_pairwise_denominator_is_cohort_restricted(monkeypatch):
    """The pair universe is profiled[a] & profiled[b], intersected with the cohort."""
    samples = [f"s{i}" for i in range(1, 11)]
    events = [_ev(s, "TP53") for s in ["s1", "s2", "s5"]] + [_ev(s, "KRAS") for s in ["s1", "s6"]]
    clinical = [
        (s, "p_" + s, "CANCER_TYPE", BREAST if s in {"s1", "s2", "s3", "s4"} else LUNG)
        for s in samples
    ]
    _setup(monkeypatch, events=events, wes_samples=samples, clinical=clinical)

    p = server._build_cooccurrence_payload(
        "study", ["TP53", "KRAS"], ["mutation"], cohort={"CANCER_TYPE": [BREAST]}
    )

    assert "error" not in p
    pair = p["pairs"][0]
    # Breast is s1-s4: TP53 in s1,s2; KRAS in s1 only (s6 is lung).
    assert pair["n_profiled"] == 4
    assert (pair["n_both"], pair["n_a_only"], pair["n_b_only"], pair["n_neither"]) == (1, 1, 0, 2)
    assert p["cohort"]["n_samples"] == 4


def test_lollipop_counts_only_cohort_samples(monkeypatch):
    events = [
        {"sample_unique_id": s, "mutation_variant": "p.R175H", "mutation_type": "Missense_Mutation"}
        for s in ["s1", "s2", "s5"]
    ]
    clinical = [
        (s, "p_" + s, "CANCER_TYPE", BREAST if s in {"s1", "s2"} else LUNG)
        for s in ["s1", "s2", "s5"]
    ]
    _setup(monkeypatch, events=events, clinical=clinical)

    p = server._build_lollipop_payload("study", "TP53", cohort={"CANCER_TYPE": [BREAST]})

    assert "error" not in p
    assert p["n_samples_mutated"] == 2
    assert p["mutations"][0]["count"] == 2
    assert p["cohort"]["n_samples"] == 2


# --- trap 2: sample-grain attribute vs patient-grain survival ----------------


def test_ambiguous_patient_is_excluded_and_counted(monkeypatch):
    """p_a has one breast and one lung sample, so its cohort membership is undefined.

    It must be excluded from the curve and the exclusion surfaced, not guessed.
    """
    clinical = [
        ("s1", "p_a", "CANCER_TYPE", BREAST),
        ("s2", "p_a", "CANCER_TYPE", LUNG),  # same patient, disagreeing samples
        ("s3", "p_b", "CANCER_TYPE", BREAST),
        ("s4", "p_c", "CANCER_TYPE", BREAST),
    ]
    survival = [
        {"patient_unique_id": "p_a", "time": "10", "status": "1:DECEASED"},
        {"patient_unique_id": "p_b", "time": "20", "status": "1:DECEASED"},
        {"patient_unique_id": "p_c", "time": "30", "status": "0:LIVING"},
    ]
    _setup(monkeypatch, clinical=clinical, survival=survival)

    p = server._build_survival_payload(
        "study", "OS", None, None, None, cohort={"CANCER_TYPE": [BREAST]}
    )

    assert "error" not in p
    assert p["cohort"]["n_patients"] == 2, "the multi-primary patient must be excluded"
    assert p["groups"][0]["n_patients"] == 2
    assert any("conflicting CANCER_TYPE" in w for w in p["warnings"])


def test_cohort_patient_ids_reports_ambiguity_count(monkeypatch):
    clinical = [
        ("s1", "p_a", "CANCER_TYPE", BREAST),
        ("s2", "p_a", "CANCER_TYPE", LUNG),
        ("s3", "p_b", "CANCER_TYPE", BREAST),
    ]
    _setup(monkeypatch, clinical=clinical)

    matched, n_ambiguous = server._cohort_patient_ids("study", {"CANCER_TYPE": [BREAST]})

    assert matched == {"p_b"}
    assert n_ambiguous == 1


# --- zero-match filters must error, not render an empty chart ----------------


def test_zero_match_cohort_errors_in_survival(monkeypatch):
    clinical = [("s1", "p_a", "CANCER_TYPE", LUNG)]
    survival = [{"patient_unique_id": "p_a", "time": "10", "status": "1:DECEASED"}]
    _setup(monkeypatch, clinical=clinical, survival=survival)

    p = server._build_survival_payload(
        "study", "OS", None, None, None, cohort={"CANCER_TYPE": ["Nonexistent Type"]}
    )

    assert "Cohort filter matched no patients with survival data" in p["error"]
    assert p["groups"] == []


@pytest.mark.parametrize(
    "builder",
    [
        lambda c: server._build_oncoprint_payload("study", ["TP53"], ["mutation"], [], None, c),
        lambda c: server._build_cooccurrence_payload("study", ["TP53", "KRAS"], ["mutation"], c),
        lambda c: server._build_lollipop_payload("study", "TP53", c),
    ],
    ids=["oncoprint", "cooccurrence", "lollipop"],
)
def test_zero_match_cohort_errors_in_sample_grain_apps(monkeypatch, builder):
    clinical = [("s1", "p_s1", "CANCER_TYPE", LUNG)]
    _setup(monkeypatch, clinical=clinical, wes_samples=["s1"])

    p = builder({"CANCER_TYPE": ["Nonexistent Type"]})

    assert "Cohort filter matched no samples" in p["error"]


def test_invalid_attribute_is_rejected_by_the_tool(monkeypatch):
    # The validator must reject at the tool boundary, not produce a wrong answer.
    _setup(monkeypatch)
    result = server.survival_curve(study_id="study", cohort={"BAD NAME!": ["x"]})
    assert "Invalid attribute name" in result["error"]


def test_compound_cohort_is_rejected_by_the_tool(monkeypatch):
    _setup(monkeypatch)
    result = server.oncoprint(study_id="study", cohort={"CANCER_TYPE": [BREAST], "SEX": ["Female"]})
    assert "exactly one attribute" in result["error"]


# --- disclosure when no filter is supplied -----------------------------------


def test_unfiltered_multi_cancer_study_warns(monkeypatch):
    survival = [
        {"patient_unique_id": "p_a", "time": "10", "status": "1:DECEASED"},
        {"patient_unique_id": "p_b", "time": "20", "status": "0:LIVING"},
    ]
    _setup(monkeypatch, survival=survival, cancer_type_spread=(25040, 42))

    p = server._build_survival_payload("study", "OS", None, None, None, cohort=None)

    assert p["cohort"]["unfiltered"] is True
    assert p["cohort"]["filter"] is None
    expected = "No cohort filter applied: spans 25040 samples across 42 cancer types."
    assert p["cohort"]["warning"] == expected
    # Also in warnings[], which every widget already renders -- no rebuild needed.
    assert expected in p["warnings"]


def test_unfiltered_single_cancer_study_does_not_warn(monkeypatch):
    survival = [{"patient_unique_id": "p_a", "time": "10", "status": "1:DECEASED"}]
    _setup(monkeypatch, survival=survival, cancer_type_spread=(1084, 1))

    p = server._build_survival_payload("study", "OS", None, None, None, cohort=None)

    assert p["cohort"]["unfiltered"] is True
    assert "warning" not in p["cohort"]
    assert not any("No cohort filter applied" in w for w in p["warnings"])


def test_every_data_app_payload_carries_a_cohort_block(monkeypatch):
    samples = ["s1", "s2"]
    events = [_ev(s, "TP53") for s in samples] + [_ev("s1", "KRAS")]
    lolli = [
        {"sample_unique_id": s, "mutation_variant": "p.R175H", "mutation_type": "Missense_Mutation"}
        for s in samples
    ]
    survival = [
        {"patient_unique_id": "p_s1", "time": "10", "status": "1:DECEASED"},
        {"patient_unique_id": "p_s2", "time": "20", "status": "0:LIVING"},
    ]

    monkeypatch.setattr(
        server,
        "run_select_query",
        _fake_db(events=events, wes_samples=samples, survival=survival, cancer_type_spread=(2, 1)),
    )
    assert "cohort" in server._build_survival_payload("study", "OS", None, None, None)
    assert "cohort" in server._build_oncoprint_payload("study", ["TP53"], ["mutation"], [], None)
    assert "cohort" in server._build_cooccurrence_payload("study", ["TP53", "KRAS"], ["mutation"])

    monkeypatch.setattr(
        server,
        "run_select_query",
        _fake_db(events=lolli, wes_samples=samples, cancer_type_spread=(2, 1)),
    )
    assert "cohort" in server._build_lollipop_payload("study", "TP53")


def test_all_four_tools_expose_a_cohort_argument():
    import inspect

    for tool in (
        server.survival_curve,
        server.oncoprint,
        server.mutation_diagram,
        server.alteration_cooccurrence,
    ):
        params = inspect.signature(tool).parameters
        assert "cohort" in params, f"{tool.__name__} is missing the cohort argument"
        assert params["cohort"].default is None
