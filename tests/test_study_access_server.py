"""Study access on the server paths main added after #88: the shared
list_studies snapshot, get_study_guide's parallel section queries, and the
ClickHouse settings sent with each SELECT."""

import contextvars
from unittest.mock import patch

import pytest

from cbioportal_mcp import server
from cbioportal_mcp.authentication.study_access import clickhouse_study_access_settings
from cbioportal_mcp.env import McpConfig

_REQUEST_HEADERS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "headers", default=None
)


def _request_scoped_headers(include_all=False):
    # Mimics fastmcp: headers live in a contextvar of the request's context,
    # so a worker thread that doesn't copy the context sees none.
    return _REQUEST_HEADERS.get() or {}


@pytest.fixture
def restricted(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.delenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", raising=False)
    _REQUEST_HEADERS.set({"x-user-id": "alice", "x-cbioportal-allowed-studies": "study_alpha"})
    with patch("fastmcp.server.dependencies.get_http_headers", _request_scoped_headers):
        yield


def _study(study_id):
    return {
        "cancer_study_identifier": study_id,
        "name": study_id,
        "description": "",
        "type_of_cancer_id": "brca",
        "sample_count": 1,
    }


def test_list_studies_filters_shared_snapshot_per_request(restricted, monkeypatch):
    calls = []

    def fake_run_select_query(query, query_label=None, **kwargs):
        calls.append(kwargs)
        return [_study("study_alpha"), _study("study_beta")]

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn()

    assert [row["cancer_study_identifier"] for row in rows] == ["study_alpha"]
    # The snapshot is shared and refreshed off-request, so it is fetched
    # unrestricted and never through the request's own study guard.
    assert calls == [{"all_studies": True}]
    server._clear_studies_cache()


def test_get_study_guide_section_queries_keep_request_context(restricted, monkeypatch):
    seen = []

    def fake_run_select_query(query, query_label=None, **kwargs):
        if query_label != "study_guide.study_info":
            # Same check the real run_select_query does first.
            server.guard_query_study_access(query)
        seen.append(query_label)
        if query_label == "study_guide.study_info":
            return [_study("study_alpha")]
        return []

    monkeypatch.setattr(server, "_load_study_guide", lambda study_id: None)
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    guide = server.get_study_guide.fn("study_alpha")

    assert not guide.startswith("Error"), guide
    assert "study_guide.counts" in seen and "study_guide.top_genes" in seen


def test_all_studies_settings_skip_request_resolution(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        # No identity at all (background thread): still resolvable.
        assert clickhouse_study_access_settings(McpConfig(), all_studies=True) == {
            "SQL_cbiomcp_allowed_studies": "*"
        }


def test_request_settings_carry_allowed_studies(restricted, monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
    assert clickhouse_study_access_settings(McpConfig()) == {
        "SQL_cbiomcp_allowed_studies": "study_alpha"
    }


def _allow(studies: str) -> None:
    _REQUEST_HEADERS.set({"x-user-id": "alice", "x-cbioportal-allowed-studies": studies})


def _no_db(query, query_label=None, **kwargs):
    raise AssertionError(f"curated guide path must not query the database: {query_label}")


@pytest.mark.parametrize("requested", ["MSK_CHORD_2024", "msk_chord_2024", "Msk_Chord_2024"])
def test_curated_guide_allowed_for_canonical_acl_in_any_casing(restricted, monkeypatch, requested):
    monkeypatch.setattr(server, "run_select_query", _no_db)
    _allow("msk_chord_2024")

    guide = server.get_study_guide.fn(requested)

    assert not guide.startswith("Error"), guide[:200]


@pytest.mark.parametrize("requested", ["MSK_CHORD_2024", "msk_chord_2024"])
def test_curated_guide_denied_for_non_canonical_acl_spelling(restricted, monkeypatch, requested):
    # An ACL entry 'MSK_CHORD_2024' is not the study msk_chord_2024.
    monkeypatch.setattr(server, "run_select_query", _no_db)
    _allow("MSK_CHORD_2024")

    guide = server.get_study_guide.fn(requested)

    assert guide == "Error: Not authorized to access study 'msk_chord_2024'."


@pytest.mark.parametrize("requested", ["STUDY_ALPHA", "study_alpha"])
def test_dynamic_guide_allowed_for_canonical_acl_in_any_casing(restricted, monkeypatch, requested):
    def fake_run_select_query(query, query_label=None, **kwargs):
        if query_label == "study_guide.study_info":
            return [_study("study_alpha")]
        server.guard_query_study_access(query)
        return []

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)
    _allow("study_alpha")

    guide = server.get_study_guide.fn(requested)

    assert guide.startswith("# Study Guide"), guide[:200]


@pytest.mark.parametrize("requested", ["STUDY_BETA", "study_beta"])
def test_dynamic_guide_denied_for_non_canonical_acl_spelling(restricted, monkeypatch, requested):
    seen = []

    def fake_run_select_query(query, query_label=None, **kwargs):
        seen.append(query_label)
        return [_study("study_beta")] if query_label == "study_guide.study_info" else []

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)
    _allow("STUDY_BETA")

    guide = server.get_study_guide.fn(requested)

    assert guide == "Error: Not authorized to access study 'study_beta'."
    assert seen == ["study_guide.study_info"]  # no section queries ran
