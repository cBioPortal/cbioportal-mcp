"""Tests for the mutation lollipop tool (``mutation_diagram``), its helpers, and
the data contract.

The database is faked by monkeypatching ``run_select_query`` on the server
module (the lollipop only issues one per-gene query against
``genomic_event_derived``), so these run without a live ClickHouse instance.
"""

import pytest

from cbioportal_mcp import server, ui

# --- pure helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("p.V600E", 600),
        ("V600E", 600),
        ("p.R175H", 175),
        ("p.T790M", 790),
        ("p.G12D", 12),
        ("p.E746_A750del", 746),  # first codon of an in-frame deletion
        ("p.R213*", 213),  # nonsense
        ("p.K27fs", 27),  # frameshift
        ("p.*307L", 307),  # readthrough
        ("NA", None),
        ("", None),
        (None, None),
        ("p.(=)", None),  # silent, no codon number
        ("p.0?", None),  # position 0 is not a valid codon
    ],
)
def test_parse_protein_position(value, expected):
    assert server._parse_protein_position(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("p.V600E", "V600E"),
        ("V600E", "V600E"),
        ("p.E746_A750del", "E746_A750del"),
        ("  p.R175H  ", "R175H"),
        ("P.G12D", "G12D"),  # case-insensitive prefix
    ],
)
def test_clean_protein_change(value, expected):
    assert server._clean_protein_change(value) == expected


# --- fake DB -----------------------------------------------------------------


def _mut_row(sample, variant, mtype="Missense_Mutation"):
    """One genomic_event_derived mutation row (omitting empty fields like the
    real zip_select_query_result does)."""
    row = {"sample_unique_id": sample}
    if variant is not None:
        row["mutation_variant"] = variant
    if mtype is not None:
        row["mutation_type"] = mtype
    return row


def _fake_mutations(rows):
    """run_select_query stand-in: returns ``rows`` for the per-gene mutation
    query, nothing otherwise."""

    def fake_query(q):
        if "FROM genomic_event_derived" in q:
            return rows
        return []

    return fake_query


# --- _build_lollipop_payload -------------------------------------------------


def test_payload_aggregates_counts_and_sorts(monkeypatch):
    rows = [
        _mut_row("s1", "p.V600E"),
        _mut_row("s2", "p.V600E"),
        _mut_row("s3", "p.V600E"),
        _mut_row("s4", "p.R175H"),
        _mut_row("s5", "p.E746_A750del", "In_Frame_Del"),
        _mut_row("s6", "p.R213*", "Nonsense_Mutation"),
    ]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "BRAF")

    assert "error" not in p
    assert p["gene"] == "BRAF"
    # Sorted by recurrence desc, then position: V600E(3) first.
    assert p["mutations"][0] == {
        "protein_change": "V600E",
        "position": 600,
        "count": 3,
        "class": "missense",
        "types": ["Missense_Mutation"],
    }
    by_change = {m["protein_change"]: m for m in p["mutations"]}
    assert by_change["R213*"]["class"] == "truncating" and by_change["R213*"]["position"] == 213
    assert by_change["E746_A750del"]["class"] == "inframe"
    assert p["protein_change_count"] == 4
    assert p["n_samples_mutated"] == 6
    assert p["max_position"] == 746
    assert p["class_counts"] == {"missense": 2, "truncating": 1, "inframe": 1}
    assert p["unmapped_count"] == 0
    assert p["warnings"] == []


def test_payload_counts_distinct_samples_not_rows(monkeypatch):
    # A sample carrying the same change twice counts once; a sample with two
    # different changes is counted once overall but in each change's recurrence.
    rows = [
        _mut_row("s1", "p.V600E"),
        _mut_row("s1", "p.V600E"),  # duplicate event for same sample
        _mut_row("s1", "p.K601E"),  # s1 also has a second change
        _mut_row("s2", "p.V600E"),
    ]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "BRAF")
    by_change = {m["protein_change"]: m for m in p["mutations"]}
    assert by_change["V600E"]["count"] == 2  # s1, s2 (not 3)
    assert by_change["K601E"]["count"] == 1
    assert p["n_samples_mutated"] == 2  # s1, s2


def test_payload_merges_most_severe_class_for_a_change(monkeypatch):
    # Same protein change reported under two MAF types -> most severe class wins,
    # and both types are recorded.
    rows = [
        _mut_row("s1", "p.X100_splice", "Missense_Mutation"),
        _mut_row("s2", "p.X100_splice", "Splice_Site"),
    ]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "TP53")
    m = p["mutations"][0]
    assert m["class"] == "truncating"  # Splice_Site outranks Missense
    assert m["types"] == ["Missense_Mutation", "Splice_Site"]
    assert m["count"] == 2


def test_payload_unmapped_counted_not_plotted(monkeypatch):
    rows = [
        _mut_row("s1", "p.V600E"),
        _mut_row("s2", "NA", "Splice_Region"),  # NA -> unmapped
        _mut_row("s3", None, "Splice_Region"),  # missing variant -> unmapped
    ]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "BRAF")
    assert [m["protein_change"] for m in p["mutations"]] == ["V600E"]
    assert p["unmapped_count"] == 2
    assert p["n_samples_mutated"] == 3  # unmapped samples still counted
    assert any("without a plottable protein position" in w for w in p["warnings"])


def test_payload_no_mutations_errors(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _fake_mutations([]))
    p = server._build_lollipop_payload("study", "BRAF")
    assert "error" in p
    assert p["mutations"] == []


def test_payload_all_unmapped_errors(monkeypatch):
    rows = [_mut_row("s1", "NA", "Splice_Region"), _mut_row("s2", "NA", "Splice_Region")]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "TERT")
    assert "error" in p
    assert "plottable protein-change position" in p["error"]
    # Even on this error path, the mutated-sample count is reported.
    assert p["n_samples_mutated"] == 2
    assert p["unmapped_count"] == 2


def test_payload_truncates_to_cap_and_warns(monkeypatch):
    n = server.MAX_LOLLIPOP_MUTATIONS + 5
    rows = [_mut_row(f"s{i}", f"p.G{i}D") for i in range(1, n + 1)]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    p = server._build_lollipop_payload("study", "BIG")
    assert len(p["mutations"]) == server.MAX_LOLLIPOP_MUTATIONS
    assert p["protein_change_count"] == n  # full distinct count still reported
    assert any("most recurrent" in w for w in p["warnings"])


def test_fetch_lollipop_mutations_query_shape(monkeypatch):
    seen = {}

    def spy(q):
        seen["q"] = q
        return []

    monkeypatch.setattr(server, "run_select_query", spy)
    server._fetch_lollipop_mutations("brca_tcga", "TP53")
    q = seen["q"]
    # Per-gene, mutation-only, excluding UNCALLED; no CNA/SV predicates.
    assert "hugo_gene_symbol = 'TP53'" in q
    assert "cancer_study_identifier = 'brca_tcga'" in q
    assert "variant_type = 'mutation'" in q
    assert "mutation_status != 'UNCALLED'" in q
    assert "cna_alteration" not in q
    assert "structural_variant" not in q


# --- mutation_diagram tool wrapper (error contract) --------------------------


def test_mutation_diagram_bad_study_id():
    out = server.mutation_diagram("bad id!", "TP53")
    assert "error" in out
    assert out["mutations"] == []
    assert out["study_id"] == "bad id!" and out["gene"] == "TP53"


def test_mutation_diagram_bad_gene(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _fake_mutations([]))
    out = server.mutation_diagram("valid_study", "bad gene!")
    assert "error" in out
    assert out["mutations"] == []


def test_mutation_diagram_happy_path(monkeypatch):
    rows = [_mut_row("s1", "p.V600E"), _mut_row("s2", "p.V600E"), _mut_row("s3", "p.R175H")]
    monkeypatch.setattr(server, "run_select_query", _fake_mutations(rows))
    out = server.mutation_diagram("valid_study", "BRAF")
    assert "error" not in out
    assert out["gene"] == "BRAF"
    assert out["mutations"][0]["protein_change"] == "V600E"
    assert out["mutations"][0]["count"] == 2


# --- UI wiring (ui:// URI, CSP allowlist, widget HTML) -----------------------


def test_lollipop_app_config_allows_genome_nexus():
    cfg = ui.lollipop_app_config()
    assert cfg.resource_uri == ui.LOLLIPOP_UI_URI == "ui://cbioportal/lollipop"
    # The lollipop is the one widget that reaches the network: its CSP must
    # allow connecting to Genome Nexus (and only that).
    assert cfg.csp is not None
    assert cfg.csp.connect_domains == [ui.GENOME_NEXUS_ORIGIN]
    assert ui.GENOME_NEXUS_ORIGIN == "https://www.genomenexus.org"
    assert cfg.visibility == ["model"]


def test_lollipop_widget_html_loads():
    html = ui.load_widget("lollipop.html")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "Widget asset not found" not in html
