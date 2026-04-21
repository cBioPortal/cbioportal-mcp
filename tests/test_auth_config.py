"""Tests for authentication configuration in McpConfig and AuthMode enum."""

import json

import pytest

from cbioportal_mcp.env import AuthMode, McpConfig


class TestAuthModeEnum:
    """Tests for the AuthMode enum."""

    def test_enum_values(self):
        """AuthMode enum should contain exactly 'none', 'static', and 'jwt'."""
        assert AuthMode.NONE.value == "none"
        assert AuthMode.STATIC.value == "static"
        assert AuthMode.JWT.value == "jwt"

    def test_enum_values_classmethod(self):
        """AuthMode.values() should return a list of all valid string values."""
        values = AuthMode.values()
        assert set(values) == {"none", "static", "jwt"}
        assert len(values) == 3

    def test_enum_is_str(self):
        """AuthMode members should be usable as strings (str, Enum mixin)."""
        assert isinstance(AuthMode.NONE, str)
        assert AuthMode.NONE == "none"


class TestMcpConfigAuthMode:
    """Tests for the mcp_auth_mode property on McpConfig."""

    def test_default_auth_mode_is_none(self, monkeypatch):
        """When MCP_AUTH_MODE is unset, the default should be 'none'."""
        monkeypatch.delenv("MCP_AUTH_MODE", raising=False)
        config = McpConfig()
        assert config.mcp_auth_mode == "none"

    def test_static_auth_mode(self, monkeypatch):
        """Setting MCP_AUTH_MODE=static should return 'static'."""
        monkeypatch.setenv("MCP_AUTH_MODE", "static")
        config = McpConfig()
        assert config.mcp_auth_mode == "static"

    def test_jwt_auth_mode(self, monkeypatch):
        """Setting MCP_AUTH_MODE=jwt should return 'jwt'."""
        monkeypatch.setenv("MCP_AUTH_MODE", "jwt")
        config = McpConfig()
        assert config.mcp_auth_mode == "jwt"

    def test_auth_mode_case_insensitive(self, monkeypatch):
        """Auth mode parsing should be case-insensitive."""
        monkeypatch.setenv("MCP_AUTH_MODE", "STATIC")
        config = McpConfig()
        assert config.mcp_auth_mode == "static"

    def test_invalid_auth_mode_raises(self, monkeypatch):
        """An unrecognised auth mode should raise ValueError."""
        monkeypatch.setenv("MCP_AUTH_MODE", "oauth2")
        config = McpConfig()
        with pytest.raises(ValueError, match="Invalid auth mode"):
            _ = config.mcp_auth_mode


class TestMcpConfigAuthTokens:
    """Tests for the mcp_auth_tokens property."""

    def test_auth_tokens_parsed_from_json(self, monkeypatch):
        """MCP_AUTH_TOKENS should be parsed from a JSON string."""
        tokens = {"my-secret-token": {"client_id": "test-client", "scopes": ["read"]}}
        monkeypatch.setenv("MCP_AUTH_TOKENS", json.dumps(tokens))
        config = McpConfig()
        result = config.mcp_auth_tokens
        assert result == tokens
        assert result["my-secret-token"]["client_id"] == "test-client"

    def test_auth_tokens_returns_none_when_unset(self, monkeypatch):
        """When MCP_AUTH_TOKENS is not set, the property should return None."""
        monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
        config = McpConfig()
        assert config.mcp_auth_tokens is None

    def test_auth_tokens_returns_none_for_empty_string(self, monkeypatch):
        """When MCP_AUTH_TOKENS is an empty string, the property should return None."""
        monkeypatch.setenv("MCP_AUTH_TOKENS", "")
        config = McpConfig()
        assert config.mcp_auth_tokens is None


class TestMcpConfigJwtProperties:
    """Tests for JWT-related config properties."""

    def test_jwks_uri(self, monkeypatch):
        """mcp_auth_jwks_uri should return the MCP_AUTH_JWKS_URI env var."""
        monkeypatch.setenv("MCP_AUTH_JWKS_URI", "https://example.com/.well-known/jwks.json")
        config = McpConfig()
        assert config.mcp_auth_jwks_uri == "https://example.com/.well-known/jwks.json"

    def test_jwks_uri_none_when_unset(self, monkeypatch):
        """mcp_auth_jwks_uri should return None when the env var is unset."""
        monkeypatch.delenv("MCP_AUTH_JWKS_URI", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwks_uri is None

    def test_jwt_issuer(self, monkeypatch):
        """mcp_auth_jwt_issuer should return the MCP_AUTH_JWT_ISSUER env var."""
        monkeypatch.setenv("MCP_AUTH_JWT_ISSUER", "https://auth.example.com/")
        config = McpConfig()
        assert config.mcp_auth_jwt_issuer == "https://auth.example.com/"

    def test_jwt_issuer_none_when_unset(self, monkeypatch):
        """mcp_auth_jwt_issuer should return None when the env var is unset."""
        monkeypatch.delenv("MCP_AUTH_JWT_ISSUER", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_issuer is None

    def test_jwt_audience(self, monkeypatch):
        """mcp_auth_jwt_audience should return the MCP_AUTH_JWT_AUDIENCE env var."""
        monkeypatch.setenv("MCP_AUTH_JWT_AUDIENCE", "my-api")
        config = McpConfig()
        assert config.mcp_auth_jwt_audience == "my-api"

    def test_jwt_audience_none_when_unset(self, monkeypatch):
        """mcp_auth_jwt_audience should return None when the env var is unset."""
        monkeypatch.delenv("MCP_AUTH_JWT_AUDIENCE", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_audience is None

    def test_jwt_secret(self, monkeypatch):
        """mcp_auth_jwt_secret should return the MCP_AUTH_JWT_SECRET env var."""
        monkeypatch.setenv("MCP_AUTH_JWT_SECRET", "super-secret-key")
        config = McpConfig()
        assert config.mcp_auth_jwt_secret == "super-secret-key"

    def test_jwt_secret_none_when_unset(self, monkeypatch):
        """mcp_auth_jwt_secret should return None when the env var is unset."""
        monkeypatch.delenv("MCP_AUTH_JWT_SECRET", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_secret is None


class TestMcpConfigJwtAlgorithm:
    """Tests for the JWT algorithm default logic."""

    def test_default_algorithm_rs256_when_no_secret(self, monkeypatch):
        """When no JWT secret is set, the default algorithm should be RS256 (for JWKS)."""
        monkeypatch.delenv("MCP_AUTH_JWT_SECRET", raising=False)
        monkeypatch.delenv("MCP_AUTH_JWT_ALGORITHM", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_algorithm == "RS256"

    def test_default_algorithm_hs256_when_secret_set(self, monkeypatch):
        """When a JWT secret is set, the default algorithm should be HS256 (symmetric)."""
        monkeypatch.setenv("MCP_AUTH_JWT_SECRET", "my-secret")
        monkeypatch.delenv("MCP_AUTH_JWT_ALGORITHM", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_algorithm == "HS256"

    def test_explicit_algorithm_overrides_default(self, monkeypatch):
        """An explicit MCP_AUTH_JWT_ALGORITHM should override the default."""
        monkeypatch.setenv("MCP_AUTH_JWT_ALGORITHM", "ES256")
        monkeypatch.delenv("MCP_AUTH_JWT_SECRET", raising=False)
        config = McpConfig()
        assert config.mcp_auth_jwt_algorithm == "ES256"


class TestValidateAuthConfig:
    """Tests for McpConfig.validate_auth_config()."""

    def test_validate_raises_for_static_without_tokens(self, monkeypatch):
        """Static mode without MCP_AUTH_TOKENS should raise ValueError."""
        monkeypatch.setenv("MCP_AUTH_MODE", "static")
        monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
        config = McpConfig()
        with pytest.raises(ValueError, match="MCP_AUTH_TOKENS is not set"):
            config.validate_auth_config()

    def test_validate_raises_for_jwt_without_credentials(self, monkeypatch):
        """JWT mode without JWKS URI or secret should raise ValueError."""
        monkeypatch.setenv("MCP_AUTH_MODE", "jwt")
        monkeypatch.delenv("MCP_AUTH_JWKS_URI", raising=False)
        monkeypatch.delenv("MCP_AUTH_JWT_SECRET", raising=False)
        config = McpConfig()
        with pytest.raises(ValueError, match="neither MCP_AUTH_JWKS_URI nor"):
            config.validate_auth_config()

    def test_validate_passes_for_none_mode(self, monkeypatch):
        """Validation should pass without errors when mode is 'none'."""
        monkeypatch.setenv("MCP_AUTH_MODE", "none")
        config = McpConfig()
        config.validate_auth_config()  # Should not raise

    def test_validate_passes_for_static_with_tokens(self, monkeypatch):
        """Validation should pass when static mode has tokens configured."""
        monkeypatch.setenv("MCP_AUTH_MODE", "static")
        tokens = {"token123": {"client_id": "client1", "scopes": ["read"]}}
        monkeypatch.setenv("MCP_AUTH_TOKENS", json.dumps(tokens))
        config = McpConfig()
        config.validate_auth_config()  # Should not raise

    def test_validate_passes_for_jwt_with_jwks_uri(self, monkeypatch):
        """Validation should pass when jwt mode has a JWKS URI."""
        monkeypatch.setenv("MCP_AUTH_MODE", "jwt")
        monkeypatch.setenv("MCP_AUTH_JWKS_URI", "https://example.com/.well-known/jwks.json")
        monkeypatch.delenv("MCP_AUTH_JWT_SECRET", raising=False)
        config = McpConfig()
        config.validate_auth_config()  # Should not raise

    def test_validate_passes_for_jwt_with_secret(self, monkeypatch):
        """Validation should pass when jwt mode has a symmetric secret."""
        monkeypatch.setenv("MCP_AUTH_MODE", "jwt")
        monkeypatch.setenv("MCP_AUTH_JWT_SECRET", "my-secret-key")
        monkeypatch.delenv("MCP_AUTH_JWKS_URI", raising=False)
        config = McpConfig()
        config.validate_auth_config()  # Should not raise
