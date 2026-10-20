import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cbioportal_mcp.authentication.study_access import (
    AuthorizationError,
    StudyAccessConfigError,
    get_current_study_access,
    guard_query_study_access,
    validate_study_access_startup_config,
)
from cbioportal_mcp.env import McpConfig


def _headers(values: dict[str, str]):
    return patch("fastmcp.server.dependencies.get_http_headers", return_value=values)


def test_public_mode_allows_queries_without_study_filters(monkeypatch):
    monkeypatch.delenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", raising=False)

    with _headers({}):
        guard_query_study_access("SELECT count(*) FROM clinical_data_derived", McpConfig())


def test_restricted_mode_reads_allowed_studies_header(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a,study_b",
        }
    ):
        access = get_current_study_access(McpConfig())

    assert access.all_studies is False
    assert access.studies == {"study_a", "study_b"}


def test_restricted_mode_reads_acl_users_and_groups(monkeypatch, tmp_path: Path):
    acl_file = tmp_path / "study-acl.json"
    acl_file.write_text(
        json.dumps(
            {
                "users": {"user@example.org": ["study_user"]},
                "groups": {"/team/brca": ["study_group"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACL_FILE", str(acl_file))

    with _headers(
        {
            "x-user-email": "user@example.org",
            "x-forwarded-groups": "/team/brca",
        }
    ):
        access = get_current_study_access(McpConfig())

    assert access.studies == {"study_user", "study_group"}


def test_restricted_mode_rejects_missing_identity(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers({}), pytest.raises(AuthorizationError, match="Authentication is required"):
        get_current_study_access(McpConfig())


def test_restricted_query_rejects_disallowed_study(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="study_b"):
        guard_query_study_access(
            "SELECT * FROM clinical_data_derived WHERE cancer_study_identifier = 'study_b'",
            McpConfig(),
        )


def test_restricted_query_requires_literal_study_filter(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="explicit literal"):
        guard_query_study_access("SELECT * FROM clinical_data_derived", McpConfig())


def test_clickhouse_row_policy_mode_allows_indirect_protected_queries(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ):
        guard_query_study_access("SELECT * FROM mutation ORDER BY mutation_event_id", McpConfig())


def test_clickhouse_row_policy_mode_allows_two_hop_indirect_queries(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ):
        guard_query_study_access(
            "SELECT * FROM clinical_event_data ORDER BY clinical_event_id", McpConfig()
        )


def test_restricted_query_rejects_indirect_table_without_row_policies(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="explicit literal"):
        guard_query_study_access("SELECT * FROM clinical_event_data", McpConfig())


def test_clickhouse_row_policy_mode_still_rejects_explicit_denied_study(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="study_b"):
        guard_query_study_access(
            "SELECT * FROM clinical_data_derived WHERE cancer_study_identifier = 'study_b'",
            McpConfig(),
        )


def test_clickhouse_row_policy_mode_rejects_setting_override(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="internal ClickHouse study allowlist"):
        guard_query_study_access(
            "SELECT * FROM mutation SETTINGS SQL_cbiomcp_allowed_studies = 'study_b'",
            McpConfig(),
        )


def test_restricted_query_rejects_boolean_bypass_shape(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ), pytest.raises(AuthorizationError, match="simple study-scoped"):
        guard_query_study_access(
            "SELECT * FROM clinical_data_derived "
            "WHERE cancer_study_identifier = 'study_a' OR 1 = 1",
            McpConfig(),
        )


def test_proxy_secret_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cr3t")

    with _headers({"x-user-id": "user-1"}), pytest.raises(
        AuthorizationError, match="trusted-proxy secret"
    ):
        get_current_study_access(McpConfig())


def test_proxy_secret_rejects_wrong_value(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cr3t")

    with _headers(
        {"x-user-id": "user-1", "x-cbioportal-mcp-proxy-secret": "wrong"}
    ), pytest.raises(AuthorizationError, match="trusted-proxy secret"):
        get_current_study_access(McpConfig())


def test_proxy_secret_accepts_matching_value(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cr3t")

    with _headers(
        {
            "x-user-id": "user-1",
            "x-cbioportal-mcp-proxy-secret": "s3cr3t",
            "x-cbioportal-allowed-studies": "study_a",
        }
    ):
        access = get_current_study_access(McpConfig())

    assert access.studies == {"study_a"}


def test_startup_config_rejects_restricted_mode_without_proxy_secret(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.delenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", raising=False)

    with pytest.raises(StudyAccessConfigError, match="CBIOPORTAL_MCP_AUTH_PROXY_SECRET"):
        validate_study_access_startup_config(McpConfig())


def test_startup_config_allows_public_mode_without_proxy_secret(monkeypatch):
    monkeypatch.delenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", raising=False)
    monkeypatch.delenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", raising=False)

    validate_study_access_startup_config(McpConfig())


def test_startup_config_warns_when_row_policies_disabled(monkeypatch, caplog):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cr3t")
    monkeypatch.delenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", raising=False)

    with caplog.at_level("WARNING"):
        validate_study_access_startup_config(McpConfig())

    assert any("row-policy" in record.message.lower() for record in caplog.records)


def test_startup_config_no_warning_when_row_policies_enabled(monkeypatch, caplog):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cr3t")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")

    with caplog.at_level("WARNING"):
        validate_study_access_startup_config(McpConfig())

    assert not caplog.records
