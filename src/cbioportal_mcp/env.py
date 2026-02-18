"""Environment configuration for cBioPortal MCP server."""

from dataclasses import dataclass
import json
import os
from typing import Optional
from enum import Enum


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid transport values."""
        return [transport.value for transport in cls]


class AuthMode(str, Enum):
    """Supported authentication modes."""

    NONE = "none"
    STATIC = "static"
    JWT = "jwt"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid auth mode values."""
        return [mode.value for mode in cls]


@dataclass
class McpConfig:
    """Configuration for Mcp connection settings."""

    def __init__(self):
        """Initialize the configuration from environment variables."""

    @property
    def mcp_server_transport(self) -> str:
        """Get the MCP server transport method.

        Valid options: "stdio", "http", "sse"
        Default: "stdio"
        """
        transport = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()

        # Validate transport type
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid options: {valid_options}")
        return transport

    @property
    def mcp_bind_host(self) -> str:
        """Get the host to bind the MCP server to.

        Only used when transport is "http" or "sse".
        Default: "127.0.0.1"
        """
        return os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")

    @property
    def mcp_bind_port(self) -> int:
        """Get the port to bind the MCP server to.

        Only used when transport is "http" or "sse".
        Default: 8000
        """
        return int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", "8000"))

    @property
    def mcp_user(self) -> str:
        """Get the clickhouse user for which the MCP server is running for.

        Default: "app_user"
        """

        return str(os.getenv("CLICKHOUSE_USER", "app_user"))

    @property
    def mcp_database(self) -> str:
        """Get the clickhouse db for which the MCP server is running against

        Default: "cgds_public_2025_06_24"
        """

        return str(os.getenv("CLICKHOUSE_DATABASE", "cgds_public_2025_06_24"))

    @property
    def mcp_auth_mode(self) -> str:
        """Get the authentication mode.

        Valid options: "none", "static", "jwt"
        - 'static': Uses static bearer tokens from MCP_AUTH_TOKENS env var
        - 'jwt': Uses JWT verification via JWKS or symmetric key
        - 'none': No authentication (default)
        """
        mode = os.getenv("MCP_AUTH_MODE", "none").lower()

        # Validate auth mode
        if mode not in AuthMode.values():
            valid_options = ", ".join(f'"{m}"' for m in AuthMode.values())
            raise ValueError(f"Invalid auth mode '{mode}'. Valid options: {valid_options}")
        return mode

    @property
    def mcp_auth_tokens(self) -> dict[str, dict] | None:
        """Get static tokens for 'static' auth mode.

        Format: JSON string mapping token -> {client_id, scopes}
        Env var: MCP_AUTH_TOKENS
        """
        raw = os.getenv("MCP_AUTH_TOKENS")
        if not raw:
            return None
        return json.loads(raw)

    @property
    def mcp_auth_jwks_uri(self) -> str | None:
        """Get the JWKS URI for JWT token verification.

        Env var: MCP_AUTH_JWKS_URI
        """
        return os.getenv("MCP_AUTH_JWKS_URI")

    @property
    def mcp_auth_jwt_issuer(self) -> str | None:
        """Get the expected JWT issuer claim.

        Env var: MCP_AUTH_JWT_ISSUER
        """
        return os.getenv("MCP_AUTH_JWT_ISSUER")

    @property
    def mcp_auth_jwt_audience(self) -> str | None:
        """Get the expected JWT audience claim.

        Env var: MCP_AUTH_JWT_AUDIENCE
        """
        return os.getenv("MCP_AUTH_JWT_AUDIENCE")

    @property
    def mcp_auth_jwt_secret(self) -> str | None:
        """Get the symmetric secret for HMAC JWT verification (alternative to JWKS).

        Env var: MCP_AUTH_JWT_SECRET
        """
        return os.getenv("MCP_AUTH_JWT_SECRET")

    @property
    def mcp_auth_jwt_algorithm(self) -> str:
        """Get the JWT signing algorithm.

        Default: RS256 (for JWKS) or HS256 (for symmetric secret).
        Env var: MCP_AUTH_JWT_ALGORITHM
        """
        default = "HS256" if self.mcp_auth_jwt_secret else "RS256"
        return os.getenv("MCP_AUTH_JWT_ALGORITHM", default)

    def validate_auth_config(self) -> None:
        """Validate authentication configuration.

        Raises:
            ValueError: If auth mode is 'jwt' but neither JWKS URI nor JWT secret
                is provided, or if auth mode is 'static' but no tokens are provided.
        """
        mode = self.mcp_auth_mode

        if mode == AuthMode.STATIC.value:
            if not self.mcp_auth_tokens:
                raise ValueError(
                    "Auth mode is 'static' but MCP_AUTH_TOKENS is not set. "
                    "Provide a JSON mapping of token -> {client_id, scopes}."
                )

        if mode == AuthMode.JWT.value:
            if not self.mcp_auth_jwks_uri and not self.mcp_auth_jwt_secret:
                raise ValueError(
                    "Auth mode is 'jwt' but neither MCP_AUTH_JWKS_URI nor "
                    "MCP_AUTH_JWT_SECRET is set. Provide at least one."
                )


# Global instance placeholders for the singleton pattern
_MCP_CONFIG_INSTANCE = None


def get_mcp_config():
    """
    Gets the singleton instance of McpConfig.
    Instantiates it on the first call.
    """
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        # Instantiate the config object here, ensuring load_dotenv() has likely run
        _MCP_CONFIG_INSTANCE = McpConfig()
    return _MCP_CONFIG_INSTANCE
