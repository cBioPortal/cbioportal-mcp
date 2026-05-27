import pytest

from cbioportal_mcp.env import McpConfig


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    for env_var in [
        "CBIOPORTAL_AUTH_ENABLED",
        "CBIOPORTAL_AUTH_REQUIRED",
        "CBIOPORTAL_KEYCLOAK_CLIENT_ID",
        "CBIOPORTAL_AUTH_ISSUER",
        "CBIOPORTAL_AUTH_AUDIENCE",
        "CBIOPORTAL_AUTH_JWKS_URI",
        "CBIOPORTAL_AUTH_PUBLIC_KEY",
        "CBIOPORTAL_AUTH_JWT_ALGORITHM",
        "CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV",
        "CBIOPORTAL_ALL_STUDIES_ROLE",
        "CBIOPORTAL_DEFAULT_STUDIES",
        "CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
        "CBIOPORTAL_CLICKHOUSE_ALLOW_ALL_SETTING",
        "CBIOPORTAL_MCP_PROFILE",
    ]:
        monkeypatch.delenv(env_var, raising=False)


def test_auth_config_defaults_are_compatible():
    config = McpConfig()

    assert config.cbioportal_auth_enabled is False
    assert config.cbioportal_auth_required is False
    assert config.cbioportal_keycloak_client_id == "cbioportal"
    assert config.cbioportal_auth_issuer == ""
    assert config.cbioportal_auth_audience == ""
    assert config.cbioportal_auth_jwks_uri == ""
    assert config.cbioportal_auth_public_key == ""
    assert config.cbioportal_auth_jwt_algorithm == "RS256"
    assert config.cbioportal_auth_allow_unverified_jwt_for_dev is False
    assert config.cbioportal_all_studies_role == "cbioportal:ALL"
    assert config.cbioportal_default_studies == ""
    assert config.cbioportal_clickhouse_allowed_studies_setting == "cbioportal_allowed_studies"
    assert config.cbioportal_clickhouse_allow_all_setting == "cbioportal_allow_all"
    assert config.cbioportal_mcp_profile == "internal"


def test_auth_config_reads_env(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "yes")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CBIOPORTAL_KEYCLOAK_CLIENT_ID", "custom-client")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ISSUER", "https://issuer.example")
    monkeypatch.setenv("CBIOPORTAL_AUTH_AUDIENCE", "cbioportal-api")
    monkeypatch.setenv("CBIOPORTAL_AUTH_JWKS_URI", "https://issuer.example/jwks")
    monkeypatch.setenv("CBIOPORTAL_AUTH_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("CBIOPORTAL_AUTH_JWT_ALGORITHM", "RS512")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV", "true")
    monkeypatch.setenv("CBIOPORTAL_ALL_STUDIES_ROLE", "all-studies")
    monkeypatch.setenv("CBIOPORTAL_DEFAULT_STUDIES", "brca_tcga")
    monkeypatch.setenv("CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING", "allowed")
    monkeypatch.setenv("CBIOPORTAL_CLICKHOUSE_ALLOW_ALL_SETTING", "allow_all")
    monkeypatch.setenv("CBIOPORTAL_MCP_PROFILE", "authenticated")

    config = McpConfig()

    assert config.cbioportal_auth_enabled is True
    assert config.cbioportal_auth_required is True
    assert config.cbioportal_keycloak_client_id == "custom-client"
    assert config.cbioportal_auth_issuer == "https://issuer.example"
    assert config.cbioportal_auth_audience == "cbioportal-api"
    assert config.cbioportal_auth_jwks_uri == "https://issuer.example/jwks"
    assert config.cbioportal_auth_public_key == "public-key"
    assert config.cbioportal_auth_jwt_algorithm == "RS512"
    assert config.cbioportal_auth_allow_unverified_jwt_for_dev is True
    assert config.cbioportal_all_studies_role == "all-studies"
    assert config.cbioportal_default_studies == "brca_tcga"
    assert config.cbioportal_clickhouse_allowed_studies_setting == "allowed"
    assert config.cbioportal_clickhouse_allow_all_setting == "allow_all"
    assert config.cbioportal_mcp_profile == "authenticated"


def test_invalid_bool_env_fails_closed(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "definitely")

    with pytest.raises(ValueError, match="Invalid boolean value"):
        _ = McpConfig().cbioportal_auth_enabled
