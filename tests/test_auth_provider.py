"""Tests for the authentication provider factory (create_auth_provider)."""

import json
from unittest.mock import MagicMock, PropertyMock

import pytest

from cbioportal_mcp.authentication.auth_provider import create_auth_provider
from fastmcp.server.auth import StaticTokenVerifier, JWTVerifier


def _make_config_mock(**overrides) -> MagicMock:
    """Build a MagicMock that behaves like McpConfig for create_auth_provider.

    All auth-related properties default to sensible "unset" values and can be
    overridden via keyword arguments.
    """
    defaults = {
        "mcp_auth_mode": "none",
        "mcp_auth_tokens": None,
        "mcp_auth_jwks_uri": None,
        "mcp_auth_jwt_secret": None,
        "mcp_auth_jwt_algorithm": "RS256",
        "mcp_auth_jwt_issuer": None,
        "mcp_auth_jwt_audience": None,
    }
    defaults.update(overrides)

    mock = MagicMock()
    # Use PropertyMock so that attribute access returns the value directly
    # rather than another MagicMock.
    for attr, value in defaults.items():
        type(mock).__dict__  # force type creation
        setattr(type(mock), attr, PropertyMock(return_value=value))

    # validate_auth_config should be a regular method (no-op by default).
    mock.validate_auth_config = MagicMock()

    return mock


class TestNoneMode:
    """Tests for auth mode 'none'."""

    def test_none_mode_returns_none(self):
        """create_auth_provider should return None when mode is 'none'."""
        config = _make_config_mock(mcp_auth_mode="none")
        result = create_auth_provider(config)
        assert result is None

    def test_none_mode_calls_validate(self):
        """Even in 'none' mode, validate_auth_config should be called."""
        config = _make_config_mock(mcp_auth_mode="none")
        create_auth_provider(config)
        config.validate_auth_config.assert_called_once()


class TestStaticMode:
    """Tests for auth mode 'static'."""

    def test_static_mode_creates_static_token_verifier(self):
        """create_auth_provider should return a StaticTokenVerifier for static mode."""
        tokens = {"secret-tok": {"client_id": "c1", "scopes": ["read"]}}
        config = _make_config_mock(mcp_auth_mode="static", mcp_auth_tokens=tokens)
        result = create_auth_provider(config)
        assert isinstance(result, StaticTokenVerifier)

    def test_static_mode_without_tokens_raises(self):
        """Static mode with tokens=None should raise ValueError."""
        config = _make_config_mock(mcp_auth_mode="static", mcp_auth_tokens=None)
        with pytest.raises(ValueError, match="MCP_AUTH_TOKENS is not set"):
            create_auth_provider(config)

    def test_static_mode_with_empty_dict_raises(self):
        """Static mode with an empty tokens dict should raise ValueError."""
        config = _make_config_mock(mcp_auth_mode="static", mcp_auth_tokens={})
        with pytest.raises(ValueError, match="MCP_AUTH_TOKENS is not set"):
            create_auth_provider(config)


class TestJwtModeWithJWKS:
    """Tests for auth mode 'jwt' using a JWKS URI."""

    def test_jwt_jwks_creates_jwt_verifier(self):
        """create_auth_provider should return a JWTVerifier when JWKS URI is set."""
        config = _make_config_mock(
            mcp_auth_mode="jwt",
            mcp_auth_jwks_uri="https://auth.example.com/.well-known/jwks.json",
            mcp_auth_jwt_secret=None,
            mcp_auth_jwt_algorithm="RS256",
            mcp_auth_jwt_issuer="https://auth.example.com/",
            mcp_auth_jwt_audience="my-api",
        )
        result = create_auth_provider(config)
        assert isinstance(result, JWTVerifier)

    def test_jwt_jwks_calls_validate(self):
        """Validation should be invoked before building the verifier."""
        config = _make_config_mock(
            mcp_auth_mode="jwt",
            mcp_auth_jwks_uri="https://auth.example.com/.well-known/jwks.json",
            mcp_auth_jwt_secret=None,
        )
        create_auth_provider(config)
        config.validate_auth_config.assert_called_once()


class TestJwtModeWithSymmetricSecret:
    """Tests for auth mode 'jwt' using a symmetric (HMAC) secret."""

    def test_jwt_secret_creates_jwt_verifier(self):
        """create_auth_provider should return a JWTVerifier when a symmetric secret is set."""
        config = _make_config_mock(
            mcp_auth_mode="jwt",
            mcp_auth_jwks_uri=None,
            mcp_auth_jwt_secret="my-hmac-secret",
            mcp_auth_jwt_algorithm="HS256",
            mcp_auth_jwt_issuer=None,
            mcp_auth_jwt_audience=None,
        )
        result = create_auth_provider(config)
        assert isinstance(result, JWTVerifier)


class TestJwtModeErrorCases:
    """Tests for JWT error paths."""

    def test_jwt_without_any_credentials_raises(self):
        """JWT mode with neither JWKS nor secret should raise ValueError."""
        config = _make_config_mock(
            mcp_auth_mode="jwt",
            mcp_auth_jwks_uri=None,
            mcp_auth_jwt_secret=None,
        )
        with pytest.raises(ValueError, match="neither MCP_AUTH_JWKS_URI nor"):
            create_auth_provider(config)

    def test_jwt_with_both_jwks_and_secret_raises(self):
        """JWT mode with BOTH JWKS URI and secret should raise ValueError."""
        config = _make_config_mock(
            mcp_auth_mode="jwt",
            mcp_auth_jwks_uri="https://auth.example.com/.well-known/jwks.json",
            mcp_auth_jwt_secret="my-hmac-secret",
        )
        with pytest.raises(ValueError, match="both MCP_AUTH_JWKS_URI and"):
            create_auth_provider(config)


class TestUnknownMode:
    """Tests for unrecognised auth modes hitting the factory directly."""

    def test_unknown_mode_raises(self):
        """An unrecognised mode should raise ValueError from the factory."""
        config = _make_config_mock(mcp_auth_mode="oauth2")
        with pytest.raises(ValueError, match="Unknown auth mode"):
            create_auth_provider(config)
