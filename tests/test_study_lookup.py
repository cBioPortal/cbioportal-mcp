"""Study-identifier resolution (issue #80).

The bot told users a study "does not exist" when it was live on cbioportal.org. Two
defects fed that: the lookup was case-sensitive, and the miss message asserted
non-existence instead of describing a deployment-scoped lookup failure.
"""

import pytest

from cbioportal_mcp import server


def _guide(study_id: str) -> str:
    """Call the tool's underlying function; @mcp.tool wraps it in a FunctionTool."""
    return server.get_study_guide.fn(study_id)


CANCER_STUDY_ROW = {
    "cancer_study_identifier": "nbl_msk_2023",
    "name": "Neuroblastoma (MSK, 2023)",
    "description": "A study that exists.",
    "type_of_cancer_id": "nbl",
}


@pytest.fixture
def fake_db(monkeypatch):
    """Stand in for ClickHouse: only the exact lowercase identifier is stored."""
    seen = []

    def run_select_query(query: str, query_label=None):
        seen.append(" ".join(query.split()))
        q = query.lower()
        if "from cancer_study" in q and "lower(cancer_study_identifier) = lower(" in q:
            # The column is case-sensitive in ClickHouse; only a case-insensitive
            # comparison can match the stored lowercase identifier.
            return [CANCER_STUDY_ROW] if "nbl_msk_2023" in q else []
        if "from cancer_study" in q and "like" in q:
            return [
                {"cancer_study_identifier": "nbl_target_2018", "name": "Neuroblastoma (TARGET)"}
            ]
        return []

    monkeypatch.setattr(server, "run_select_query", run_select_query)
    return seen


def test_lookup_is_case_insensitive(fake_db):
    """'NBL_MSK_2023' must resolve to the stored lowercase study."""
    out = _guide("NBL_MSK_2023")
    assert "does not exist" not in out
    assert "did not match any study" not in out
    assert "Neuroblastoma (MSK, 2023)" in out


def test_downstream_sections_use_the_canonical_identifier(fake_db):
    """After resolving, later queries must use the DB's spelling, not the user's."""
    _guide("NBL_MSK_2023")
    followups = [q for q in fake_db if "clinical_data_derived" in q]
    assert followups, "expected the cohort-statistics query to run"
    for q in followups:
        assert "'nbl_msk_2023'" in q, f"query kept the user's casing: {q}"


def test_miss_message_never_asserts_nonexistence(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", lambda q, query_label=None: [])
    out = _guide("totally_made_up_study")

    assert "does not exist" in out  # only as the instruction NOT to say it
    assert "Do not tell the user that 'totally_made_up_study' does not exist." in out
    assert "did not match any study in the database" in out


def test_miss_message_lists_the_three_recoverable_causes(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", lambda q, query_label=None: [])
    out = _guide("nbl_msk_9999")

    assert "Different identifier" in out
    assert "Another cBioPortal instance" in out
    assert "Access restriction" in out
    assert "cbioportal://study-resolution-guide" in out


def test_miss_message_offers_similar_identifiers(fake_db):
    out = _guide("nbl_msk_9999")
    assert "similar identifier" in out
    assert "nbl_target_2018" in out


def test_similar_study_lookup_tolerates_a_db_error(monkeypatch):
    def boom(query: str, query_label=None):
        raise RuntimeError("clickhouse unavailable")

    monkeypatch.setattr(server, "run_select_query", boom)
    # A failing candidate lookup must not turn the miss message into an exception.
    assert server._similar_study_identifiers("nbl_msk_2023") == []


def test_dynamic_guide_labels_each_query_by_call_site(monkeypatch):
    """Every SELECT the dynamic guide issues must carry a distinct query_label,
    so Datadog can break down latency by call site instead of averaging all
    seven queries into one run_select_query number."""
    labels_seen = []

    def run_select_query(query: str, query_label=None):
        labels_seen.append(query_label)
        q = query.lower()
        if "from cancer_study" in q and "lower(cancer_study_identifier) = lower(" in q:
            return [CANCER_STUDY_ROW]
        return []

    monkeypatch.setattr(server, "run_select_query", run_select_query)
    _guide("nbl_msk_2023")

    assert set(labels_seen) == {
        "study_guide.study_info",
        "study_guide.counts",
        "study_guide.profiles",
        "study_guide.panels",
        "study_guide.attrs",
        "study_guide.top_genes",
        "study_guide.sample_types",
    }


def test_dynamic_guide_section_order_is_stable_despite_concurrent_completion(monkeypatch):
    """Sections 2-7 are fetched concurrently; whichever query finishes first must
    not reorder the guide's markdown sections."""
    import time as time_module

    # Deliberately finish in the reverse of section order (sample_types first,
    # counts last) to prove the assembled guide doesn't just reflect completion
    # order.
    delay_by_label = {
        "study_guide.counts": 0.05,
        "study_guide.profiles": 0.04,
        "study_guide.panels": 0.03,
        "study_guide.attrs": 0.02,
        "study_guide.top_genes": 0.01,
        "study_guide.sample_types": 0.0,
    }

    def run_select_query(query: str, query_label=None):
        q = query.lower()
        if "from cancer_study" in q and "lower(cancer_study_identifier) = lower(" in q:
            return [CANCER_STUDY_ROW]
        time_module.sleep(delay_by_label.get(query_label, 0.0))
        if query_label == "study_guide.counts":
            return [{"patient_count": 1, "sample_count": 2}]
        if query_label == "study_guide.profiles":
            return [{"genetic_alteration_type": "MUTATION_EXTENDED", "name": "Mutations"}]
        if query_label == "study_guide.panels":
            return [{"gene_panel_id": "WES", "sample_count": 2}]
        if query_label == "study_guide.attrs":
            return [{"attribute_name": "OS_MONTHS", "coverage": 2}]
        if query_label == "study_guide.top_genes":
            return [{"hugo_gene_symbol": "TP53", "altered_samples": 1}]
        if query_label == "study_guide.sample_types":
            return [{"sample_type": "Primary", "count": 2}]
        return []

    monkeypatch.setattr(server, "run_select_query", run_select_query)
    out = _guide("nbl_msk_2023")

    sections = [
        "## Cohort Statistics",
        "## Available Data Types",
        "## Gene Panels",
        "## Available Clinical Attributes",
        "## Top Mutated Genes",
        "## Sample Types",
    ]
    positions = [out.index(s) for s in sections]
    assert positions == sorted(positions), (
        "guide sections were reordered by which query finished first"
    )


def test_similar_study_lookup_ignores_short_tokens(monkeypatch):
    def fail_if_called(q, query_label=None):
        pytest.fail("should not query")

    monkeypatch.setattr(server, "run_select_query", fail_if_called)
    assert server._similar_study_identifiers("a_b") == []


def test_static_guide_resolves_case_insensitively():
    """A real guide on disk must be found regardless of the requested casing."""
    available = server._list_available_study_guides()
    assert available, "expected bundled study guides"
    name = available[0]
    assert server._load_study_guide(name) is not None
    assert server._load_study_guide(name.upper()) is not None


def test_unknown_static_guide_still_returns_none():
    assert server._load_study_guide("no_such_study_guide_xyz") is None
