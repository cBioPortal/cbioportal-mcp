import pytest

from cbioportal_mcp.authentication.study_scope import (
    StudyScope,
    is_valid_study_id,
    parse_study_scope_from_claims,
)
from cbioportal_mcp.env import McpConfig


def _claims(client_id: str = "cbioportal", roles: list[str] | None = None) -> dict:
    return {
        "resource_access": {
            client_id: {
                "roles": roles or [],
            }
        }
    }


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    env_vars = [
        "CBIOPORTAL_AUTH_ENABLED",
        "CBIOPORTAL_AUTH_REQUIRED",
        "CBIOPORTAL_KEYCLOAK_CLIENT_ID",
        "CBIOPORTAL_ALL_STUDIES_ROLE",
        "CBIOPORTAL_DEFAULT_STUDIES",
        "CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
        "CBIOPORTAL_CLICKHOUSE_ALLOW_ALL_SETTING",
        "CBIOPORTAL_MCP_PROFILE",
    ]
    for env_var in env_vars:
        monkeypatch.delenv(env_var, raising=False)


def test_no_claims_auth_disabled_allows_all():
    scope = parse_study_scope_from_claims(None, McpConfig())

    assert scope.allow_all is True
    assert scope.allowed_studies == frozenset()
    assert scope.source == "auth_disabled"


def test_auth_disabled_does_not_validate_unused_default_studies(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_DEFAULT_STUDIES", "bad study")

    scope = parse_study_scope_from_claims(None, McpConfig())

    assert scope.allow_all is True
    assert scope.source == "auth_disabled"


def test_no_claims_auth_required_has_no_access(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "true")

    scope = parse_study_scope_from_claims(None, McpConfig())

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset()
    assert scope.source == "no_claims"


def test_valid_study_roles(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")

    scope = parse_study_scope_from_claims(
        _claims(roles=["brca_tcga", "luad-tcga", "Study123"]),
        McpConfig(),
    )

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset({"brca_tcga", "luad-tcga", "Study123"})
    assert scope.source == "keycloak_role"


def test_invalid_study_role_strings_are_ignored(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")

    scope = parse_study_scope_from_claims(
        _claims(roles=["brca_tcga", "bad study", "bad;study", "", "../bad"]),
        McpConfig(),
    )

    assert scope.allowed_studies == frozenset({"brca_tcga"})


def test_duplicate_roles_are_deduplicated(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")

    scope = parse_study_scope_from_claims(
        _claims(roles=["brca_tcga", "brca_tcga"]),
        McpConfig(),
    )

    assert scope.allowed_studies == frozenset({"brca_tcga"})


def test_all_studies_role_allows_all(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")

    scope = parse_study_scope_from_claims(
        _claims(roles=["brca_tcga", "cbioportal:ALL"]),
        McpConfig(),
    )

    assert scope.allow_all is True
    assert scope.allowed_studies == frozenset()
    assert scope.source == "keycloak_role"


def test_roles_under_non_default_client_id(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_KEYCLOAK_CLIENT_ID", "custom-client")

    scope = parse_study_scope_from_claims(
        _claims(client_id="custom-client", roles=["brca_tcga"]),
        McpConfig(),
    )

    assert scope.allowed_studies == frozenset({"brca_tcga"})


def test_roles_under_wrong_client_id_are_ignored(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_KEYCLOAK_CLIENT_ID", "custom-client")

    scope = parse_study_scope_from_claims(
        _claims(client_id="cbioportal", roles=["brca_tcga"]),
        McpConfig(),
    )

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset()


def test_configured_default_studies(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_DEFAULT_STUDIES", "brca_tcga, luad-tcga")

    scope = parse_study_scope_from_claims(None, McpConfig())

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset({"brca_tcga", "luad-tcga"})
    assert scope.source == "default_studies"


def test_empty_access_means_no_access(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")

    scope = parse_study_scope_from_claims(_claims(roles=[]), McpConfig())

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset()
    assert scope.source == "keycloak_role"


def test_study_scope_rejects_invalid_study_ids():
    with pytest.raises(ValueError, match="Invalid study_id"):
        StudyScope(allowed_studies=frozenset({"brca_tcga", "bad study"}))


def test_study_id_validation_matches_expected_character_set():
    assert is_valid_study_id("brca_tcga")
    assert is_valid_study_id("luad-tcga")
    assert is_valid_study_id("Study123")
    assert not is_valid_study_id("bad study")
    assert not is_valid_study_id("bad.study")
    assert not is_valid_study_id("")
