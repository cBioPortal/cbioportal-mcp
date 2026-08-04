"""Environment configuration for cBioPortal MCP server."""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid transport values."""
        return [transport.value for transport in cls]


class StudyAccessMode(str, Enum):
    """Supported end-user study access modes."""

    PUBLIC = "public"
    RESTRICTED = "restricted"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid study access modes."""
        return [mode.value for mode in cls]


def _parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


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
    def study_access_mode(self) -> str:
        """Get the end-user study access mode.

        ``public`` keeps public cBioPortal behavior: all studies visible.
        ``restricted`` resolves allowed studies from headers and/or an ACL file.
        """
        mode = os.getenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", StudyAccessMode.PUBLIC.value).lower()
        if mode in {"off", "disabled", "none"}:
            mode = StudyAccessMode.PUBLIC.value
        if mode not in StudyAccessMode.values():
            valid_options = ", ".join(f'"{m}"' for m in StudyAccessMode.values())
            raise ValueError(f"Invalid study access mode '{mode}'. Valid options: {valid_options}")
        return mode

    @property
    def auth_required(self) -> bool:
        """Whether request identity headers are required.

        Restricted study access usually needs an authenticated reverse proxy.
        Public mode defaults to false; restricted mode defaults to true.
        """
        default = self.study_access_mode == StudyAccessMode.RESTRICTED.value
        return _parse_bool_env("CBIOPORTAL_MCP_AUTH_REQUIRED", default)

    @property
    def study_acl_file(self) -> Optional[str]:
        """Path to a JSON ACL mapping users/groups to allowed study IDs."""
        value = os.getenv("CBIOPORTAL_MCP_STUDY_ACL_FILE")
        return value if value else None

    @property
    def allowed_studies_header(self) -> str:
        """Header carrying a comma-separated or JSON array list of allowed studies."""
        return os.getenv(
            "CBIOPORTAL_MCP_ALLOWED_STUDIES_HEADER",
            "x-cbioportal-allowed-studies",
        ).lower()

    @property
    def global_allowed_studies(self) -> str:
        """Static comma-separated allowed studies for service-scoped deployments."""
        return os.getenv("CBIOPORTAL_MCP_ALLOWED_STUDIES", "")

    @property
    def auth_admin_groups(self) -> str:
        """Comma-separated groups that receive access to all studies."""
        return os.getenv("CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS", "")

    @property
    def auth_proxy_secret(self) -> Optional[str]:
        """Shared secret expected from the trusted auth proxy, when configured."""
        value = os.getenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET")
        return value if value else None

    @property
    def auth_proxy_secret_header(self) -> str:
        """Header used to carry the trusted auth proxy shared secret."""
        return os.getenv(
            "CBIOPORTAL_MCP_AUTH_PROXY_SECRET_HEADER",
            "x-cbioportal-mcp-proxy-secret",
        ).lower()

    @property
    def clickhouse_row_policy_enabled(self) -> bool:
        """Whether ClickHouse row policies enforce per-request study access.

        When enabled, the MCP server passes the resolved study allowlist to
        ClickHouse as a query setting. Row policies can then filter rows even
        for tables that do not carry a direct cancer_study_identifier column.
        """
        return _parse_bool_env("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", False)

    @property
    def clickhouse_allowed_studies_setting(self) -> str:
        """ClickHouse custom setting name containing the request study allowlist."""
        return os.getenv(
            "CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
            "SQL_cbiomcp_allowed_studies",
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
