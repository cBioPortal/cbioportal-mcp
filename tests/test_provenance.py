"""Tests for the query-provenance block on the four data-app payloads.

A chart in a chat is not reproducible unless the payload says which query
produced it. These tests pin that the emitted SQL is what actually executed --
captured, not reconstructed from the tool arguments -- and that the capture is
scoped to one payload build so calls cannot bleed into each other.

The database is faked by monkeypatching ``run_select_query`` on the server
module, dispatching on the SQL text (same approach as ``test_cohort_filter.py``).
"""

import threading

import pytest

from cbioportal_mcp import __version__, server

STUDY = "msk_chord_2024"
SAMPLES = ["s1", "s2", "s3"]


def _spy_db(events=None, survival=None):
    """run_select_query stand-in that also records every SQL string it receives.

    Returns ``(fake_query, executed)``; ``executed`` is the ground truth the
    payload's provenance is compared against.
    """
    events = events or []
    survival = survival or []
    executed: list[str] = []

    def fake_query(q):
        executed.append(q)
        if "FROM mutation_wes_coverage" in q:
            return [{"sample_unique_id": s} for s in SAMPLES]
        if "FROM mutation_panel_gene_coverage" in q:
            return []
        if "FROM genomic_event_derived" in q:
            if "COUNT(DISTINCT" in q:  # top-genes resolver
                return [{"hugo_gene_symbol": g} for g in ("TP53", "KRAS")]
            return events
        if "FROM clinical_data_derived" in q:
            if "n_cancer_types" in q:  # the unfiltered-spread probe
                return [{"n_samples": len(SAMPLES), "n_cancer_types": 2}]
            if "_MONTHS" in q:
                return survival
            return []
        return []

    return fake_query, executed


def _events():
    """genomic_event_derived rows serving the oncoprint / cooccurrence / lollipop."""
    rows = []
    for sample in SAMPLES:
        for gene in ("TP53", "KRAS"):
            rows.append(
                {
                    "sample_unique_id": sample,
                    "patient_unique_id": "p_" + sample,
                    "hugo_gene_symbol": gene,
                    "variant_type": "mutation",
                    "mutation_type": "Missense_Mutation",
                    "mutation_variant": "p.R175H",
                }
            )
    return rows


def _survival():
    return [
        {"patient_unique_id": "p_s1", "time": "10", "status": "1:DECEASED"},
        {"patient_unique_id": "p_s2", "time": "20", "status": "0:LIVING"},
        {"patient_unique_id": "p_s3", "time": "30", "status": "1:DECEASED"},
    ]


def _setup(monkeypatch, **kwargs):
    fake_query, executed = _spy_db(**kwargs)
    monkeypatch.setattr(server, "run_select_query", fake_query)
    return executed


# Each entry calls one data-app tool over the same fake study.
TOOL_CALLS = {
    "survival_curve": lambda: server.survival_curve(study_id=STUDY),
    "oncoprint": lambda: server.oncoprint(study_id=STUDY, genes=["TP53", "KRAS"]),
    "mutation_diagram": lambda: server.mutation_diagram(study_id=STUDY, gene="TP53"),
    "alteration_cooccurrence": lambda: server.alteration_cooccurrence(
        study_id=STUDY, genes=["TP53", "KRAS"]
    ),
}


@pytest.fixture
def _db(monkeypatch):
    return _setup(monkeypatch, events=_events(), survival=_survival())


# --- the block is present, populated and honest -------------------------------


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
def test_every_data_app_payload_carries_provenance_queries(_db, tool):
    payload = TOOL_CALLS[tool]()

    assert "error" not in payload, payload.get("error")
    queries = payload["provenance"]["queries"]
    assert queries, f"{tool} emitted no provenance queries"
    assert all(isinstance(q, str) for q in queries)
    # Every data-app query is study-scoped, so the study id must appear in each.
    assert all(STUDY in q for q in queries)


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
def test_provenance_is_the_sql_that_executed(_db, tool):
    """Captured verbatim, in execution order -- not rebuilt from the arguments.

    Comparing against what the fake DB received is the whole point: a payload
    that pretty-printed or reconstructed its SQL could report a query that never
    ran, which is worse than reporting none.
    """
    payload = TOOL_CALLS[tool]()

    assert payload["provenance"]["queries"] == _db


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
def test_provenance_reports_the_server_version(_db, tool):
    assert TOOL_CALLS[tool]()["provenance"]["server_version"] == __version__


def test_provenance_carries_sql_strings_only(_db):
    """SQL only, never result rows -- provenance must not bloat the payload."""
    queries = server.oncoprint(study_id=STUDY, genes=["TP53", "KRAS"])["provenance"]["queries"]

    assert all(q.lstrip().startswith("SELECT") for q in queries)
    # A row that leaked into the block would show up as its sample id; the SQL
    # itself only names studies, genes and attributes.
    assert not any(sample in q for q in queries for sample in SAMPLES)


# --- scoping ------------------------------------------------------------------


def test_provenance_does_not_accumulate_across_calls(monkeypatch):
    executed = _setup(monkeypatch, events=_events(), survival=_survival())

    first = server.mutation_diagram(study_id=STUDY, gene="TP53")
    n_first = len(executed)
    second = server.mutation_diagram(study_id=STUDY, gene="TP53")

    assert second["provenance"]["queries"] == executed[n_first:]
    assert len(second["provenance"]["queries"]) == len(first["provenance"]["queries"])


def test_capture_is_scoped_to_the_calling_thread(monkeypatch):
    """These tools are sync, so FastMCP runs them in worker threads.

    A module-level list would let one tool call record into another's payload;
    the ContextVar must keep each build's queries to itself.
    """
    monkeypatch.setattr(server, "run_select_query", lambda q: [])

    with server._record_queries() as captured:
        server._query(f"SELECT 1 FROM t WHERE study = '{STUDY}'")
        other = threading.Thread(target=lambda: server._query("SELECT 2 FROM elsewhere"))
        other.start()
        other.join()

    assert captured == [f"SELECT 1 FROM t WHERE study = '{STUDY}'"]


def test_query_outside_a_payload_build_records_nothing(monkeypatch):
    # Queries from the non-app tools (list_studies, get_study_guide, ...) must
    # not be captured, and _query must work with no capture active.
    monkeypatch.setattr(server, "run_select_query", lambda q: [{"ok": 1}])

    assert server._query("SELECT 1") == [{"ok": 1}]
    assert server._recorded_queries.get() is None


# --- error paths --------------------------------------------------------------


def test_error_payload_still_reports_the_queries_that_ran(monkeypatch):
    """A cohort filter matching nothing is exactly when the SQL is worth seeing."""
    executed = _setup(monkeypatch, events=_events(), survival=_survival())

    payload = server.mutation_diagram(
        study_id=STUDY, gene="TP53", cohort={"CANCER_TYPE": ["Nonexistent Type"]}
    )

    assert "Cohort filter matched no samples" in payload["error"]
    assert payload["provenance"]["queries"] == executed
    assert executed, "the cohort lookup should have run before the error"


def test_validation_error_returns_before_any_query(monkeypatch):
    # Rejected at the tool boundary, so nothing executed and there is nothing to
    # reproduce; the error keeps the minimal contract shape.
    executed = _setup(monkeypatch)

    payload = server.mutation_diagram(study_id="bad id!", gene="TP53")

    assert "error" in payload
    assert "provenance" not in payload
    assert executed == []
