"""Tests for the alteration co-occurrence tool (``alteration_cooccurrence``),
its data-shaping helpers, and the UI wiring.

The database is faked by monkeypatching ``run_select_query`` on the server
module. The payload issues three kinds of query (optionally a top-genes
resolution, the per-gene events, and the two coverage views), so the fake
dispatches on the SQL text.
"""

import pytest

from cbioportal_mcp import server, ui

# --- fake DB -----------------------------------------------------------------


def _ev(sample, gene, variant_type="mutation", mutation_type="Missense_Mutation", cna=None):
    """One genomic_event_derived row for the events query."""
    row = {
        "sample_unique_id": sample,
        "patient_unique_id": "p_" + sample,
        "hugo_gene_symbol": gene,
        "variant_type": variant_type,
    }
    if mutation_type is not None:
        row["mutation_type"] = mutation_type
    if cna is not None:
        row["cna_alteration"] = cna
    return row


def _fake_db(events, wes_samples, panel_rows=None, top_genes=None):
    """run_select_query stand-in dispatching on the query text."""

    def fake_query(q):
        if "FROM mutation_wes_coverage" in q:
            return [{"sample_unique_id": s} for s in wes_samples]
        if "FROM mutation_panel_gene_coverage" in q:
            return panel_rows or []
        if "FROM genomic_event_derived" in q:
            if "COUNT(DISTINCT" in q:  # the default top-genes resolver
                return top_genes or []
            return events
        return []

    return fake_query


def _setup(monkeypatch, events, wes_samples, panel_rows=None, top_genes=None):
    monkeypatch.setattr(
        server, "run_select_query", _fake_db(events, wes_samples, panel_rows, top_genes)
    )


WES10 = [f"s{i}" for i in range(1, 11)]  # s1..s10, all WES-profiled


# --- _build_cooccurrence_payload ---------------------------------------------


def test_payload_basic_contingency(monkeypatch):
    # TP53 altered in s1-s4, KRAS in s1,s2,s5 -> both=2, A-only=2, B-only=1, neither=5.
    events = (
        [_ev(s, "TP53") for s in ["s1", "s2", "s3", "s4"]]
        + [_ev(s, "KRAS") for s in ["s1", "s2", "s5"]]
    )
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", ["TP53", "KRAS"], None)

    assert "error" not in p
    assert p["genes"] == ["TP53", "KRAS"]
    assert len(p["pairs"]) == 1
    pair = p["pairs"][0]
    assert (pair["gene_a"], pair["gene_b"]) == ("TP53", "KRAS")
    assert pair["n_both"] == 2
    assert pair["n_a_only"] == 2
    assert pair["n_b_only"] == 1
    assert pair["n_neither"] == 5
    assert pair["n_profiled"] == 10
    assert pair["tendency"] == "Co-occurrence"  # a(2) > expected(1.2)
    assert pair["log2_odds_ratio"] > 0
    assert 0.0 <= pair["p_value"] <= 1.0
    assert 0.0 <= pair["q_value"] <= 1.0
    assert isinstance(pair["significant"], bool)


def test_payload_mutual_exclusivity(monkeypatch):
    events = (
        [_ev(s, "TP53") for s in ["s1", "s2", "s3"]]
        + [_ev(s, "KRAS") for s in ["s4", "s5", "s6"]]
    )
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", ["TP53", "KRAS"], None)
    pair = p["pairs"][0]
    assert pair["n_both"] == 0
    assert pair["tendency"] == "Mutual exclusivity"
    assert pair["log2_odds_ratio"] < 0


def test_payload_gene_stats(monkeypatch):
    events = [_ev(s, "TP53") for s in ["s1", "s2", "s3", "s4"]] + [_ev("s1", "KRAS")]
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", ["TP53", "KRAS"], None)
    by_gene = {g["gene"]: g for g in p["gene_stats"]}
    assert by_gene["TP53"]["altered"] == 4
    assert by_gene["TP53"]["profiled"] == 10
    assert by_gene["TP53"]["freq_pct"] == 40.0
    assert by_gene["KRAS"]["altered"] == 1


def test_payload_all_pairs_for_three_genes(monkeypatch):
    events = (
        [_ev(s, "A") for s in ["s1", "s2", "s3"]]
        + [_ev(s, "B") for s in ["s1", "s2"]]
        + [_ev(s, "C") for s in ["s4"]]
    )
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", ["A", "B", "C"], None)
    assert len(p["pairs"]) == 3  # AB, AC, BC
    # q-values are present on every pair and ordered by p-value ascending.
    assert all("q_value" in pair for pair in p["pairs"])
    pvals = [pair["p_value"] for pair in p["pairs"]]
    assert pvals == sorted(pvals)


def test_payload_dedupes_and_caps_genes(monkeypatch):
    genes = ["TP53", "tp53"] + [f"G{i}" for i in range(20)]  # dupes + over the cap
    events = [_ev("s1", "TP53")]
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", genes, None)
    # "TP53" and "tp53" are distinct symbols (validator is case-preserving); the
    # list is just de-duplicated exactly and capped.
    assert len(p["genes"]) == server.MAX_COOCCURRENCE_GENES
    assert p["genes"] == (["TP53", "tp53"] + [f"G{i}" for i in range(20)])[
        : server.MAX_COOCCURRENCE_GENES
    ]


def test_payload_fewer_than_two_genes_errors(monkeypatch):
    _setup(monkeypatch, [_ev("s1", "TP53")], WES10)
    p = server._build_cooccurrence_payload("study", ["TP53"], None)
    assert "error" in p
    assert p["genes"] == ["TP53"]
    assert p["pairs"] == []


def test_payload_no_shared_profiled_samples_errors(monkeypatch):
    # No WES samples and no panel coverage -> empty universe for the pair.
    _setup(monkeypatch, [_ev("s1", "TP53"), _ev("s1", "KRAS")], wes_samples=[])
    p = server._build_cooccurrence_payload("study", ["TP53", "KRAS"], None)
    assert "error" in p
    assert "no profiled samples" in p["error"]


def test_payload_default_genes_resolution(monkeypatch):
    top = [{"hugo_gene_symbol": g} for g in ["TP53", "KRAS"]]
    events = [_ev("s1", "TP53"), _ev("s1", "KRAS"), _ev("s2", "TP53")]
    _setup(monkeypatch, events, WES10, top_genes=top)
    p = server._build_cooccurrence_payload("study", None, None)
    assert p["genes"] == ["TP53", "KRAS"]
    assert len(p["pairs"]) == 1


def test_payload_invalid_alteration_type_raises(monkeypatch):
    _setup(monkeypatch, [], WES10)
    with pytest.raises(ValueError):
        server._build_cooccurrence_payload("study", ["TP53", "KRAS"], ["not_a_type"])


def test_payload_mutation_only_has_no_coverage_warning(monkeypatch):
    events = [_ev("s1", "TP53"), _ev("s1", "KRAS")]
    _setup(monkeypatch, events, WES10)
    p = server._build_cooccurrence_payload("study", ["TP53", "KRAS"], ["mutation"])
    assert p["alteration_types"] == ["mutation"]
    assert not any("copy-number" in w.lower() for w in p["warnings"])


# --- _cooccurrence_pair ------------------------------------------------------


def test_cooccurrence_pair_returns_none_on_empty_universe():
    altered = {"A": {"s1"}, "B": {"s1"}}
    profiled = {"A": {"s1"}, "B": set()}  # disjoint -> empty intersection
    assert server._cooccurrence_pair("A", "B", altered, profiled) is None


# --- alteration_cooccurrence tool (error contract) ---------------------------


def test_tool_bad_study_id():
    out = server.alteration_cooccurrence("bad id!", ["TP53", "KRAS"])
    assert "error" in out
    assert out["genes"] == [] and out["pairs"] == []
    assert out["study_id"] == "bad id!"


def test_tool_invalid_alteration_type_returns_error(monkeypatch):
    _setup(monkeypatch, [], WES10)
    out = server.alteration_cooccurrence("valid_study", ["TP53", "KRAS"], ["bogus"])
    assert "error" in out
    assert out["pairs"] == []


def test_tool_happy_path(monkeypatch):
    events = [_ev(s, "TP53") for s in ["s1", "s2"]] + [_ev("s1", "KRAS")]
    _setup(monkeypatch, events, WES10)
    out = server.alteration_cooccurrence("valid_study", ["TP53", "KRAS"])
    assert "error" not in out
    assert len(out["pairs"]) == 1


# --- UI wiring ---------------------------------------------------------------


def test_cooccurrence_app_config():
    cfg = ui.cooccurrence_app_config()
    assert cfg.resource_uri == ui.COOCCURRENCE_UI_URI == "ui://cbioportal/cooccurrence"
    assert cfg.visibility == ["model"]
    # Self-contained widget: no network, so no CSP connect allowlist.
    assert cfg.csp is None


def test_cooccurrence_widget_html_loads():
    html = ui.load_widget("cooccurrence.html")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "Widget asset not found" not in html
