"""Environment configuration for cBioPortal MCP server."""

import os
from dataclasses import dataclass
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
    def cbioportal_auth_enabled(self) -> bool:
        """Whether cBioPortal study authorization is enabled.

        Default: false
        """
        return _env_bool("CBIOPORTAL_AUTH_ENABLED", default=False)

    @property
    def cbioportal_auth_required(self) -> bool:
        """Whether unauthenticated HTTP/SSE requests should fail closed.

        Request enforcement is wired in a later authorization PR. This flag is
        exposed now so scope parsing can distinguish required auth from
        compatibility mode.

        Default: false
        """
        return _env_bool("CBIOPORTAL_AUTH_REQUIRED", default=False)

    @property
    def cbioportal_keycloak_client_id(self) -> str:
        """Get the Keycloak client ID containing cBioPortal study roles.

        Default: "cbioportal"
        """
        return os.getenv("CBIOPORTAL_KEYCLOAK_CLIENT_ID", "cbioportal")

    @property
    def cbioportal_auth_issuer(self) -> str:
        """Get the expected JWT issuer.

        Empty by default. When configured, verified JWTs must match this
        issuer. In dev-unverified mode this is still checked after decoding.
        """
        return os.getenv("CBIOPORTAL_AUTH_ISSUER", "")

    @property
    def cbioportal_auth_audience(self) -> str:
        """Get the expected JWT audience.

        Empty by default. When configured, decoded JWTs must include this
        audience value.
        """
        return os.getenv("CBIOPORTAL_AUTH_AUDIENCE", "")

    @property
    def cbioportal_auth_jwks_uri(self) -> str:
        """Get the JWKS URI used for JWT signature verification.

        Empty by default.
        """
        return os.getenv("CBIOPORTAL_AUTH_JWKS_URI", "")

    @property
    def cbioportal_auth_public_key(self) -> str:
        """Get a PEM public key used for JWT signature verification.

        Empty by default. Intended mainly for local tests and deployments that
        pin a static signing key.
        """
        return os.getenv("CBIOPORTAL_AUTH_PUBLIC_KEY", "")

    @property
    def cbioportal_auth_jwt_algorithm(self) -> str:
        """Get the expected JWT signing algorithm.

        Default: "RS256"
        """
        return os.getenv("CBIOPORTAL_AUTH_JWT_ALGORITHM", "RS256")

    @property
    def cbioportal_auth_allow_unverified_jwt_for_dev(self) -> bool:
        """Whether to decode unsigned/unverified JWTs for local development.

        Default: false. Do not enable this in production.
        """
        return _env_bool("CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV", default=False)

    @property
    def cbioportal_all_studies_role(self) -> str:
        """Get the role value that grants unrestricted study access.

        Default: "cbioportal:ALL"
        """
        return os.getenv("CBIOPORTAL_ALL_STUDIES_ROLE", "cbioportal:ALL")

    @property
    def cbioportal_default_studies(self) -> str:
        """Get comma-separated fallback studies for unauthenticated/default users.

        Empty by default. Parsing and validation happens in the study-scope
        module because it shares the study-ID validation contract.
        """
        return os.getenv("CBIOPORTAL_DEFAULT_STUDIES", "")

    @property
    def cbioportal_clickhouse_allowed_studies_setting(self) -> str:
        """Get the ClickHouse custom setting name for allowed studies.

        Default: "cbioportal_allowed_studies"
        """
        return os.getenv(
            "CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
            "cbioportal_allowed_studies",
        )

    @property
    def cbioportal_clickhouse_allow_all_setting(self) -> str:
        """Get the ClickHouse custom setting name for unrestricted access.

        Default: "cbioportal_allow_all"
        """
        return os.getenv(
            "CBIOPORTAL_CLICKHOUSE_ALLOW_ALL_SETTING",
            "cbioportal_allow_all",
        )

    @property
    def cbioportal_mcp_profile(self) -> str:
        """Get the MCP exposure profile.

        Reserved for later public-mode hardening. Default: "internal".
        """
        return os.getenv("CBIOPORTAL_MCP_PROFILE", "internal")


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable using common true/false spellings."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"Invalid boolean value for {name}: {raw!r}. "
        "Use one of: true/false, yes/no, on/off, 1/0."
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
