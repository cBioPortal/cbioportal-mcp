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


def test_get_study_guide_rejects_case_variant_of_unauthorized_study(restricted, monkeypatch):
    monkeypatch.setattr(server, "_load_study_guide", lambda study_id: None)
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, query_label=None, **kwargs: [_study("study_beta")],
    )
    _REQUEST_HEADERS.set({"x-user-id": "alice", "x-cbioportal-allowed-studies": "STUDY_BETA"})

    guide = server.get_study_guide.fn("STUDY_BETA")

    assert "Not authorized" in guide and "study_beta" in guide


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
