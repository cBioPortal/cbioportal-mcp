"""Environment configuration for cBioPortal MCP server."""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence


def _required_env(name: str) -> str:
    """Read an explicitly set variable; an absent value never gets a default."""
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"{name} is required and must be explicitly set")
    return value.strip()


def required_enum(name: str, choices: Sequence[str]) -> str:
    """Parse a required enum, trimming whitespace and ignoring case; no aliases."""
    value = _required_env(name).lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    return value


def required_list(name: str) -> list[str]:
    """Parse a required comma list; explicit blank means [], blank items fail.

    Preserve item case and order (e.g. user IDs), trimming surrounding spaces.
    """
    value = _required_env(name)
    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items):
        raise ValueError(f"{name} must be a comma-separated list without empty items")
    return items


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
        Default: "http"
        """
        transport = (
            os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.HTTP.value).strip().lower()
        )

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
    def mcp_http_path(self) -> Optional[str]:
        """Get the HTTP path under which to mount the MCP server.

        Set when the server is reverse-proxied behind a sub-path so that
        FastMCP's generated URLs (e.g. trailing-slash redirects) include
        the external prefix. Example: "/db/mcp" when served at
        https://host/db/mcp via Traefik without stripPrefix.

        Only used when transport is "http" or "sse". When unset, FastMCP
        uses its default path ("/mcp").
        """
        value = os.getenv("CLICKHOUSE_MCP_HTTP_PATH")
        return value if value else None

    @property
    def mcp_forwarded_allow_ips(self) -> Optional[str]:
        """Get the set of upstream IPs whose X-Forwarded-* headers uvicorn
        should trust. Forwarded as `forwarded_allow_ips` to `uvicorn.Config`.

        Set this when the server sits behind a reverse proxy that
        terminates TLS (e.g. Traefik with an https Ingress) — otherwise
        uvicorn ignores `X-Forwarded-Proto: https` and builds redirect
        Locations from its own scheme (http), which strict clients refuse
        to follow.

        Common values:
          - "*"                  trust any upstream (fine inside a Pod
                                 reachable only via in-cluster Services)
          - "10.0.0.0/8,..."     scope to a CIDR (e.g. the Traefik LB
                                 source range)

        Only used when transport is "http" or "sse". When unset, uvicorn
        defaults apply (trust only 127.0.0.1, no proxy_headers).
        """
        value = os.getenv("CLICKHOUSE_MCP_FORWARDED_ALLOW_IPS")
        return value if value else None

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
    def google_client_id(self) -> Optional[str]:
        """Google OAuth client ID (from Google Cloud Console).

        Example: "123-abc.apps.googleusercontent.com".

        One of the three Google OAuth settings gating whether OAuth is enabled
        at all — see `cbioportal_mcp.auth._build_auth_provider`. Any Google
        account can authenticate; there is no Workspace-domain restriction.
        """
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID")
        return value if value else None

    @property
    def google_client_secret(self) -> Optional[str]:
        """Google OAuth client secret (from Google Cloud Console)."""
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET")
        return value if value else None

    @property
    def google_base_url(self) -> Optional[str]:
        """This server's own public URL, used to construct its OAuth redirect/callback URIs.

        Example: "https://mcp.cbioportal.org". Must match a redirect URI
        registered on the Google OAuth client (base_url + "/auth/callback").
        """
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_BASE_URL")
        return value if value else None

    @property
    def redis_url(self) -> Optional[str]:
        """Redis connection URL for OAuth client/token storage.

        Example: "redis://:password@host:6379/1". When unset, OAuth client
        registrations and tokens (see `cbioportal_mcp.auth`) fall back to
        FastMCP's default local-disk store — fine for local dev, but that
        disk is the pod's own ephemeral filesystem in Kubernetes, so every
        pod restart silently invalidates every session.

        In production (see `knowledgesystems-k8s-deployment`'s
        `cbioagent-clickhouse-mcp-auth.yaml`) this reuses the cBioPortal
        webapp's own session-Redis instance (`cbioportal-redis-public-master`,
        credentials from the `cbioportal-public-blue` secret), on database 1
        rather than a dedicated instance — database 0 there is Spring's own
        HTTP session store (`spring.data.redis.*`), and 1-15 are otherwise
        unclaimed. Deliberately not the separate persistence-cache Redis
        instance (`redis.database=8` in the cBioPortal webapp Deployment):
        that one runs with `--redis.clear_on_startup=true`, which would wipe
        this storage on every webapp rollout and reproduce the exact bug
        this env var exists to fix. Only takes effect when OAuth itself is
        enabled (all three CBIOPORTAL_MCP_GOOGLE_* variables set).
        """
        value = os.getenv("REDIS_URL")
        return value if value else None


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


def validate_startup(transport: Optional[str] = None) -> tuple[str, str]:
    """Fail closed for configuration on construction and alternate launch paths.

    A launcher can override the transport (e.g. Inspector selects stdio), so
    validate that effective transport too, rather than trusting only the env.
    """
    filtering = required_enum("CBIOPORTAL_MCP_STUDY_FILTERING", ("on", "off"))
    if filtering == "on":
        raise ValueError(
            "CBIOPORTAL_MCP_STUDY_FILTERING=on: study filtering cannot be enabled "
            "until a later release (P11); set it to off for this release"
        )
    configured_transport = get_mcp_config().mcp_server_transport
    effective_transport = transport if transport is not None else configured_transport
    if "stdio" in (configured_transport, effective_transport):
        if os.getenv("CBIOPORTAL_MCP_LOCAL_DEV") != "1":
            raise ValueError(
                "stdio requires the local-development opt-in CBIOPORTAL_MCP_LOCAL_DEV=1"
            )
    from cbioportal_mcp import rollout  # rollout imports this module's parsers

    # Rollout switches default to off only when actually serving local stdio.
    rollout.initialize(filtering, local_dev=effective_transport == "stdio")
    from cbioportal_mcp.identity import validate_auth_startup

    validate_auth_startup()
    return filtering, effective_transport
