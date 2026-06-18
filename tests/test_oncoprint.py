"""Tests for the oncoprint tool, its helpers, and the data contract.

The database is faked by monkeypatching ``run_select_query`` on the server
module (dispatching on query substrings), so these run without a live
ClickHouse instance.
"""

import re

import pytest

from cbioportal_mcp import server

# --- pure helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation_type,expected",
    [
        ("Missense_Mutation", "missense"),
        ("Nonsense_Mutation", "truncating"),
        ("Frame_Shift_Del", "truncating"),
        ("Frame_Shift_Ins", "truncating"),
        ("Splice_Site", "truncating"),
        ("In_Frame_Del", "inframe"),
        ("In_Frame_Ins", "inframe"),
        ("Some_New_Type", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_mutation_class(mutation_type, expected):
    assert server._mutation_class(mutation_type) == expected


def test_more_severe_mut_keeps_highest_priority():
    assert server._more_severe_mut(None, "missense") == "missense"
    assert server._more_severe_mut("missense", "truncating") == "truncating"
    assert server._more_severe_mut("truncating", "missense") == "truncating"
    assert server._more_severe_mut("inframe", "other") == "inframe"


@pytest.mark.parametrize(
    "value,expected",
    [("3.2", True), ("0", True), ("-1", True), ("BRCA", False), ("", False), (None, False)],
)
def test_is_float(value, expected):
    assert server._is_float(value) is expected


def test_event_filter_reuses_alteration_configs():
    f = server._oncoprint_event_filter(["mutation", "amplification"])
    assert "variant_type = 'mutation' AND mutation_status != 'UNCALLED'" in f
    assert "variant_type = 'cna' AND cna_alteration = 2" in f
    assert " OR " in f


def test_event_filter_rejects_bad_type():
    with pytest.raises(ValueError):
        server._oncoprint_event_filter(["not_a_type"])


def test_memo_sort_clusters_alterations_top_left():
    genes = ["KRAS", "TP53"]
    cells = {
        "KRAS": {"s2": {"cna": "deepdel"}, "s4": {"mut": "missense"}},
        "TP53": {"s1": {"mut": "missense"}, "s4": {"cna": "amp"}},
    }
    not_profiled = {"TP53": {"s2"}}
    ordered = server._memo_sort(["s1", "s2", "s3", "s4"], genes, cells, not_profiled)
    # KRAS-altered samples (s2 cna=4, s4 cna+mut) come before KRAS-clean ones.
    assert ordered.index("s4") < ordered.index("s1")
    assert ordered.index("s2") < ordered.index("s1")
    # s3 (no alteration anywhere) sorts last.
    assert ordered[-1] == "s3"


# --- fake DB -----------------------------------------------------------------


def _event_row(gene, sample, patient, kind, detail=None):
    """One genomic_event_derived row (omitting empty fields, like the real zip)."""
    row = {"sample_unique_id": sample, "patient_unique_id": patient, "hugo_gene_symbol": gene}
    if kind == "mutation":
        row["variant_type"] = "mutation"
        row["mutation_type"] = detail
    elif kind == "amp":
        row["variant_type"] = "cna"
        row["cna_alteration"] = 2
    elif kind == "deepdel":
        row["variant_type"] = "cna"
        row["cna_alteration"] = -2
    elif kind == "sv":
        row["variant_type"] = "structural_variant"
    return row


def _make_fake(*, events=None, profiled=None, wes=None, top_genes=None, clinical=None):
    """Build a run_select_query stand-in dispatching on query substrings.

    profiled: {gene: [sample, ...]} panel coverage. wes: [sample, ...].
    clinical: {attr: [(level, key, value), ...]} where level is 'sample'/'patient'.
    """
    events = events or []
    profiled = profiled or {}
    wes = wes or []
    top_genes = top_genes or []
    clinical = clinical or {}

    event_rows = [_event_row(*e) for e in events]
    panel_rows = [
        {"sample_unique_id": s, "hugo_gene_symbol": g}
        for g, samples in profiled.items()
        for s in samples
    ]
    wes_rows = [{"sample_unique_id": s} for s in wes]
    rank_rows = [{"hugo_gene_symbol": g, "altered_samples": c} for g, c in top_genes]

    def _clinical_rows(attr):
        out = []
        for level, key, value in clinical.get(attr, []):
            if level == "patient":
                out.append({"patient_unique_id": key, "attribute_value": value, "type": "patient"})
            else:
                out.append(
                    {
                        "sample_unique_id": key,
                        "patient_unique_id": key,
                        "attribute_value": value,
                        "type": "sample",
                    }
                )
        return out

    def fake_query(q):
        if "AS altered_samples" in q:
            return rank_rows
        if "FROM genomic_event_derived" in q and "hugo_gene_symbol IN" in q:
            return event_rows
        if "FROM mutation_panel_gene_coverage" in q:
            return panel_rows
        if "FROM mutation_wes_coverage" in q:
            return wes_rows
        if "FROM clinical_data_derived" in q:
            m = re.search(r"attribute_name = '([^']+)'", q)
            return _clinical_rows(m.group(1)) if m else []
        return []

    return fake_query


# Shared scenario: 3 genes x 6 samples, mixed alteration types + coverage gaps.
EVENTS = [
    ("TP53", "s1", "p1", "mutation", "Missense_Mutation"),
    ("TP53", "s3", "p3", "mutation", "Nonsense_Mutation"),
    ("TP53", "s4", "p4", "amp"),
    ("KRAS", "s2", "p2", "deepdel"),
    ("KRAS", "s4", "p4", "mutation", "Missense_Mutation"),
    ("KRAS", "s5", "p5", "mutation", "Missense_Mutation"),
    ("KRAS", "s6", "p6", "sv"),
    ("PIK3CA", "s1", "p1", "amp"),
    ("PIK3CA", "s3", "p3", "mutation", "Missense_Mutation"),
]
PROFILED = {
    "TP53": ["s1", "s2", "s3", "s4", "s5"],  # not s6
    "KRAS": ["s1", "s2", "s3", "s4", "s5", "s6"],
    "PIK3CA": ["s1", "s2", "s3", "s4", "s6"],  # not s5
}
CLINICAL = {
    "CANCER_TYPE": [
        ("patient", "p1", "BRCA"),
        ("patient", "p2", "BRCA"),
        ("patient", "p3", "LUAD"),
        ("patient", "p4", "LUAD"),
        ("patient", "p5", "BRCA"),
        ("patient", "p6", "LUAD"),
    ],
    "SAMPLE_TYPE": [("sample", f"s{i}", "Primary" if i % 2 else "Metastasis") for i in range(1, 7)],
}


# --- _build_oncoprint_payload ------------------------------------------------


def test_payload_explicit_genes_full_fidelity(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", _make_fake(events=EVENTS, profiled=PROFILED, clinical=CLINICAL)
    )
    p = server._build_oncoprint_payload(
        "study", ["TP53", "KRAS", "PIK3CA"], None, ["CANCER_TYPE", "SAMPLE_TYPE"], None
    )
    assert "error" not in p
    # Rows ordered most-altered first: KRAS(4) > TP53(3) > PIK3CA(2).
    assert p["genes"] == ["KRAS", "TP53", "PIK3CA"]
    # MemoSort columns: KRAS-altered cluster first, TP53-only last.
    assert p["samples"] == ["s2", "s4", "s5", "s6", "s1", "s3"]

    # Cell typing.
    assert p["cells"]["TP53"]["s1"] == {"cna": None, "mut": "missense", "sv": False}
    assert p["cells"]["TP53"]["s3"]["mut"] == "truncating"
    assert p["cells"]["TP53"]["s4"]["cna"] == "amp"
    assert p["cells"]["KRAS"]["s2"]["cna"] == "deepdel"
    assert p["cells"]["KRAS"]["s6"]["sv"] is True
    assert p["cells"]["PIK3CA"]["s1"]["cna"] == "amp"

    # Not-profiled gray cells.
    assert p["not_profiled"]["TP53"] == ["s6"]
    assert p["not_profiled"]["PIK3CA"] == ["s5"]
    assert "KRAS" not in p["not_profiled"]

    # Per-gene stats over the full profiled set.
    stats = {g["gene"]: g for g in p["gene_stats"]}
    assert stats["KRAS"]["altered"] == 4 and stats["KRAS"]["profiled"] == 6
    assert stats["KRAS"]["freq_pct"] == 66.7
    assert stats["KRAS"]["by_type"] == {
        "mutation": 2,
        "amplification": 0,
        "deep_deletion": 1,
        "structural_variant": 1,
    }
    assert stats["TP53"]["freq_pct"] == 60.0
    assert stats["TP53"]["by_type"]["mutation"] == 2
    assert stats["TP53"]["by_type"]["amplification"] == 1
    assert stats["PIK3CA"]["freq_pct"] == 40.0

    assert p["n_samples_total"] == 6 and p["n_samples_shown"] == 6
    assert p["warnings"] == []

    # Clinical tracks (patient-level fanned out to samples; sample-level direct).
    tracks = {t["name"]: t for t in p["clinical_tracks"]}
    assert tracks["CANCER_TYPE"]["kind"] == "categorical"
    assert tracks["CANCER_TYPE"]["label"] == "Cancer Type"
    assert tracks["CANCER_TYPE"]["values"]["s1"] == "BRCA"
    assert tracks["CANCER_TYPE"]["values"]["s3"] == "LUAD"
    assert tracks["SAMPLE_TYPE"]["values"]["s1"] == "Primary"
    assert tracks["SAMPLE_TYPE"]["values"]["s2"] == "Metastasis"


def test_payload_alteration_type_filter_passed_through(monkeypatch):
    seen = {}

    fake = _make_fake(events=EVENTS, profiled=PROFILED)

    def spy(q):
        if "FROM genomic_event_derived" in q and "hugo_gene_symbol IN" in q:
            seen["events_q"] = q
        return fake(q)

    monkeypatch.setattr(server, "run_select_query", spy)
    server._build_oncoprint_payload("study", ["TP53"], ["mutation"], [], None)
    # Only the mutation predicate is in the events WHERE filter; CNA/SV predicates are not.
    assert "variant_type = 'mutation'" in seen["events_q"]
    assert "cna_alteration = 2" not in seen["events_q"]
    assert "variant_type = 'structural_variant'" not in seen["events_q"]


def test_payload_default_genes_uses_ranking_then_reorders(monkeypatch):
    # Ranking selects the gene set; row order is by actual altered count in cells.
    monkeypatch.setattr(
        server,
        "run_select_query",
        _make_fake(
            events=[
                ("TP53", "s1", "p1", "mutation", "Missense_Mutation"),
                ("TP53", "s2", "p2", "mutation", "Missense_Mutation"),
                ("KRAS", "s1", "p1", "mutation", "Missense_Mutation"),
                ("KRAS", "s2", "p2", "mutation", "Missense_Mutation"),
                ("KRAS", "s3", "p3", "mutation", "Missense_Mutation"),
            ],
            profiled={"TP53": ["s1", "s2", "s3"], "KRAS": ["s1", "s2", "s3"]},
            top_genes=[("TP53", 2), ("KRAS", 3)],
        ),
    )
    p = server._build_oncoprint_payload("study", None, None, [], None)
    assert p["genes"] == ["KRAS", "TP53"]  # KRAS(3 altered) before TP53(2)


def test_payload_truncates_and_warns(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        _make_fake(
            events=[
                ("TP53", f"s{i}", f"p{i}", "mutation", "Missense_Mutation") for i in range(1, 5)
            ],
            profiled={"TP53": [f"s{i}" for i in range(1, 5)]},
        ),
    )
    p = server._build_oncoprint_payload("study", ["TP53"], None, [], max_samples=2)
    assert p["n_samples_total"] == 4
    assert p["n_samples_shown"] == 2
    assert len(p["samples"]) == 2
    assert any("Showing 2 of 4" in w for w in p["warnings"])


def test_payload_no_coverage_data_skips_gray_cells(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        _make_fake(events=[("TP53", "s1", "p1", "mutation", "Missense_Mutation")], profiled={}),
    )
    p = server._build_oncoprint_payload("study", ["TP53"], None, [], None)
    assert p["not_profiled"] == {}
    assert p["n_samples_total"] == 1
    assert any("coverage data unavailable" in w for w in p["warnings"])


def test_payload_empty_clinical_tracks(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake(events=EVENTS, profiled=PROFILED))
    p = server._build_oncoprint_payload("study", ["TP53"], None, [], None)
    assert p["clinical_tracks"] == []


def test_payload_no_data_errors(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake())
    p = server._build_oncoprint_payload("study", ["TP53"], None, [], None)
    assert "error" in p


# --- oncoprint tool wrapper (error contract) ---------------------------------


def test_oncoprint_bad_study_id():
    out = server.oncoprint("bad id!")
    assert "error" in out
    assert out["genes"] == [] and out["samples"] == []
    assert out["study_id"] == "bad id!"


def test_oncoprint_bad_gene(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake())
    out = server.oncoprint("valid_study", genes=["bad gene!"])
    assert "error" in out


def test_oncoprint_bad_alteration_type(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake())
    out = server.oncoprint("valid_study", genes=["TP53"], alteration_types=["nonsense"])
    assert "error" in out


def test_oncoprint_happy_path(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", _make_fake(events=EVENTS, profiled=PROFILED, clinical=CLINICAL)
    )
    out = server.oncoprint("valid_study", genes=["TP53", "KRAS", "PIK3CA"])
    assert "error" not in out
    assert out["genes"] == ["KRAS", "TP53", "PIK3CA"]
    assert out["n_samples_shown"] == 6
