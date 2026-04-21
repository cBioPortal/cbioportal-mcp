"""
Authentication provider factory for the cBioPortal MCP server.

Creates the appropriate FastMCP auth provider based on McpConfig settings:
- "none"   : No authentication (returns None)
- "static" : Static bearer-token verification via StaticTokenVerifier
- "jwt"    : JWT verification via JWTVerifier (JWKS *or* symmetric secret)
"""

from __future__ import annotations

import logging
from typing import Optional

from cbioportal_mcp.env import AuthMode, McpConfig
from fastmcp.server.auth import StaticTokenVerifier, JWTVerifier, TokenVerifier

logger = logging.getLogger(__name__)


def create_auth_provider(config: McpConfig) -> Optional[TokenVerifier]:
    """Create an authentication provider based on the application configuration.

    Reads ``config.mcp_auth_mode`` and delegates to the matching FastMCP
    auth-provider class.  The caller (server startup) can pass the returned
    object directly as the ``auth`` parameter to ``FastMCP``.

    Args:
        config: The application configuration instance (``McpConfig``).

    Returns:
        A configured ``TokenVerifier`` for "static" or "jwt" modes,
        or ``None`` when authentication is disabled ("none" mode).

    Raises:
        ValueError: If the auth mode is unrecognised, or if the
            configuration for the chosen mode is incomplete/invalid
            (e.g. "static" without tokens, or "jwt" without a JWKS URI
            *and* without a symmetric secret).
    """
    # Validate early -- this will raise ValueError on bad env vars.
    config.validate_auth_config()

    mode = config.mcp_auth_mode

    # ------------------------------------------------------------------
    # No authentication
    # ------------------------------------------------------------------
    if mode == AuthMode.NONE.value:
        logger.info("Authentication disabled (mode='none').")
        return None

    # ------------------------------------------------------------------
    # Static bearer-token verification
    # ------------------------------------------------------------------
    if mode == AuthMode.STATIC.value:
        tokens = config.mcp_auth_tokens
        if not tokens:
            raise ValueError(
                "Auth mode is 'static' but MCP_AUTH_TOKENS is not set. "
                "Provide a JSON mapping of token -> {client_id, scopes}."
            )

        logger.info(
            "Authentication enabled: static token verification "
            "(%d token(s) configured).",
            len(tokens),
        )
        return StaticTokenVerifier(tokens=tokens)

    # ------------------------------------------------------------------
    # JWT verification (JWKS endpoint *or* symmetric/HMAC secret)
    # ------------------------------------------------------------------
    if mode == AuthMode.JWT.value:
        jwks_uri = config.mcp_auth_jwks_uri
        jwt_secret = config.mcp_auth_jwt_secret
        algorithm = config.mcp_auth_jwt_algorithm
        issuer = config.mcp_auth_jwt_issuer
        audience = config.mcp_auth_jwt_audience

        if not jwks_uri and not jwt_secret:
            raise ValueError(
                "Auth mode is 'jwt' but neither MCP_AUTH_JWKS_URI nor "
                "MCP_AUTH_JWT_SECRET is set. Provide at least one."
            )

        if jwks_uri and jwt_secret:
            raise ValueError(
                "Auth mode is 'jwt' but both MCP_AUTH_JWKS_URI and "
                "MCP_AUTH_JWT_SECRET are set. Provide only one."
            )

        # Build the verifier depending on which credential was supplied.
        if jwks_uri:
            logger.info(
                "Authentication enabled: JWT verification via JWKS "
                "(uri=%s, algorithm=%s).",
                jwks_uri,
                algorithm,
            )
            return JWTVerifier(
                jwks_uri=jwks_uri,
                algorithm=algorithm,
                issuer=issuer,
                audience=audience,
            )

        # Symmetric (HMAC) secret -- passed as ``public_key`` because
        # JWTVerifier uses the same parameter for both asymmetric public
        # keys and symmetric shared secrets.
        logger.info(
            "Authentication enabled: JWT verification via symmetric "
            "secret (algorithm=%s).",
            algorithm,
        )
        return JWTVerifier(
            public_key=jwt_secret,
            algorithm=algorithm,
            issuer=issuer,
            audience=audience,
        )

    # ------------------------------------------------------------------
    # Unknown mode -- should be unreachable because McpConfig validates
    # the env var, but guard defensively.
    # ------------------------------------------------------------------
    raise ValueError(
        f"Unknown auth mode '{mode}'. "
        f"Valid options: {', '.join(AuthMode.values())}."
    )
