"""Environment configuration for cBioPortal MCP server."""

import os
import re
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


class StudyAccessSource(str, Enum):
    """Where restricted mode gets each caller's study entitlements."""

    # #175 behavior: allowlist resolved in the MCP (ACL file, proxy headers,
    # admin groups) and sent to ClickHouse as a study list.
    ACL = "acl"
    # Option A: the MCP sends only the caller's verified email; ClickHouse
    # resolves the studies from cBioPortal's authorities table (user_study).
    AUTHORITIES = "authorities"
    # A claim of the access token the OIDC provider verified (provider oidc).
    JWT = "jwt"
    # The studies a cBioPortal Data Access Token can read, as the portal
    # itself lists them (provider portal_dat).
    PORTAL_DAT = "portal_dat"

    @classmethod
    def values(cls) -> list[str]:
        return [source.value for source in cls]


class AuthProvider(str, Enum):
    """How callers log in to this deployment (CBIOPORTAL_MCP_AUTH_PROVIDER)."""

    # FastMCP's GoogleProvider (the public deployment; unchanged default).
    GOOGLE = "google"
    # Any standards-based OIDC issuer (Keycloak, Okta, Entra ID, ...).
    OIDC = "oidc"
    # The caller pastes a cBioPortal Data Access Token (x-auth-token header).
    PORTAL_DAT = "portal_dat"

    @classmethod
    def values(cls) -> list[str]:
        return [provider.value for provider in cls]


class StudyAccessIdentity(str, Enum):
    """Where authorities mode takes the caller's verified email from."""

    # The MCP's own validated OAuth access token (FastMCP auth context).
    OAUTH = "oauth"
    # The trusted proxy's email header; requires CBIOPORTAL_MCP_AUTH_PROXY_SECRET.
    PROXY = "proxy"

    @classmethod
    def values(cls) -> list[str]:
        return [identity.value for identity in cls]


def _parse_header_list(name: str, default: str) -> tuple[str, ...]:
    """Comma-separated, lowercased header names from ``name`` (or ``default``)."""
    raw = os.getenv(name) or default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _optional_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bounded_float_env(name: str, default: float, low: float, high: float) -> float:
    """A float setting in [low, high]; anything else raises ValueError (fail closed)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not (low <= value <= high):  # also rejects NaN
        raise ValueError(f"{name} must be between {low:g} and {high:g}, got {raw!r}")
    return value


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
    def auth_user_id_headers(self) -> tuple[str, ...]:
        """Headers, in priority order, that carry the proxy-verified user id.

        Only these are read: anything else a client sends is ignored. The
        trusted proxy must strip all of them from inbound requests before
        injecting its own (see local_keycloak_proxy.py).
        """
        return _parse_header_list("CBIOPORTAL_MCP_AUTH_USER_ID_HEADERS", "x-user-id")

    @property
    def auth_email_headers(self) -> tuple[str, ...]:
        """Headers, in priority order, that carry the proxy-verified email."""
        return _parse_header_list("CBIOPORTAL_MCP_AUTH_EMAIL_HEADERS", "x-user-email")

    @property
    def auth_groups_headers(self) -> tuple[str, ...]:
        """Headers whose (unioned) values are the proxy-verified groups."""
        return _parse_header_list("CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS", "x-forwarded-groups")

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
    def row_policy_recheck_seconds(self) -> float:
        """How often row-policy mode re-checks views/grants after startup.

        Seconds in [1, 86400], default 300. Invalid values (NaN, inf, zero,
        negative, unparseable) raise ValueError - the server refuses to start
        in row-policy mode rather than run without the re-check.
        """
        from cbioportal_mcp.clickhouse_query_settings import parse_recheck_interval

        return parse_recheck_interval(os.getenv("CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS"))

    @property
    def clickhouse_allowed_studies_setting(self) -> str:
        """ClickHouse custom setting name containing the request study allowlist."""
        return os.getenv(
            "CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
            "SQL_cbiomcp_allowed_studies",
        )

    @property
    def study_access_source(self) -> str:
        """CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE: ``acl`` (default) or ``authorities``.

        Only consulted in restricted mode. ``authorities`` requires
        CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true and the policy set
        from sql/study-authz/authorities/.
        """
        source = (
            os.getenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", StudyAccessSource.ACL.value)
            .strip()
            .lower()
        )
        if source not in StudyAccessSource.values():
            valid = ", ".join(f'"{s}"' for s in StudyAccessSource.values())
            raise ValueError(f"Invalid study access source '{source}'. Valid options: {valid}")
        return source

    @property
    def study_access_identity(self) -> str:
        """CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY: ``oauth`` (default) or ``proxy``.

        Where authorities mode reads the caller's email. ``oauth`` uses only
        the token FastMCP validated for this request; ``proxy`` uses the
        configured email header and still requires the proxy shared secret.
        Never tool arguments, the prompt, or other headers.
        """
        identity = (
            os.getenv("CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY", StudyAccessIdentity.OAUTH.value)
            .strip()
            .lower()
        )
        if identity not in StudyAccessIdentity.values():
            valid = ", ".join(f'"{i}"' for i in StudyAccessIdentity.values())
            raise ValueError(f"Invalid study access identity '{identity}'. Valid options: {valid}")
        return identity

    @property
    def clickhouse_user_setting(self) -> str:
        """ClickHouse custom setting carrying the caller's verified email (authorities mode).

        Must match the name the policies were rendered with
        (sql/study-authz/render.sh reads the same variable).
        """
        return os.getenv("CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING", "SQL_cbio_user")

    @property
    def study_authz_identity_mode(self) -> bool:
        """True when ClickHouse resolves studies from the caller's identity."""
        return (
            self.study_access_mode == StudyAccessMode.RESTRICTED.value
            and self.study_access_source == StudyAccessSource.AUTHORITIES.value
        )

    @property
    def clickhouse_row_policy_setting(self) -> str:
        """The setting the active policy set reads (startup probes use it)."""
        if self.study_authz_identity_mode:
            return self.clickhouse_user_setting
        return self.clickhouse_allowed_studies_setting

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

    # --- login provider (CBIOPORTAL_MCP_AUTH_PROVIDER) -----------------------

    @property
    def auth_provider(self) -> str:
        """CBIOPORTAL_MCP_AUTH_PROVIDER: ``google`` (default), ``oidc`` or ``portal_dat``.

        One provider per deployment. ``google`` is exactly the pre-existing
        behavior (OAuth on only when all three CBIOPORTAL_MCP_GOOGLE_* are set).
        """
        provider = (
            os.getenv("CBIOPORTAL_MCP_AUTH_PROVIDER", AuthProvider.GOOGLE.value).strip().lower()
        )
        if provider not in AuthProvider.values():
            valid = ", ".join(f'"{p}"' for p in AuthProvider.values())
            raise ValueError(f"Invalid auth provider '{provider}'. Valid options: {valid}")
        return provider

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_base_url)

    # OIDC (provider oidc). Generic: nothing here is specific to one IdP.

    @property
    def oidc_issuer_url(self) -> Optional[str]:
        """The IdP's issuer, exactly as it appears in tokens' ``iss`` claim."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_ISSUER_URL")

    @property
    def oidc_config_url(self) -> Optional[str]:
        """Discovery document URL; default ``<issuer>/.well-known/openid-configuration``."""
        explicit = _optional_env("CBIOPORTAL_MCP_OIDC_CONFIG_URL")
        if explicit:
            return explicit
        issuer = self.oidc_issuer_url
        return issuer.rstrip("/") + "/.well-known/openid-configuration" if issuer else None

    @property
    def oidc_client_id(self) -> Optional[str]:
        return _optional_env("CBIOPORTAL_MCP_OIDC_CLIENT_ID")

    @property
    def oidc_client_secret(self) -> Optional[str]:
        return _optional_env("CBIOPORTAL_MCP_OIDC_CLIENT_SECRET")

    @property
    def oidc_base_url(self) -> Optional[str]:
        """This server's public URL (OAuth callback = base URL + /auth/callback)."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_BASE_URL")

    @property
    def oidc_audience(self) -> Optional[str]:
        """Required ``aud`` of access tokens; comma-separated values are all accepted."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_AUDIENCE")

    @property
    def oidc_audiences(self) -> list[str]:
        raw = self.oidc_audience or ""
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def oidc_scopes(self) -> list[str]:
        raw = os.getenv("CBIOPORTAL_MCP_OIDC_SCOPES") or "openid email profile"
        return [part for part in re.split(r"[\s,]+", raw) if part]

    @property
    def oidc_signing_algorithm(self) -> str:
        return (_optional_env("CBIOPORTAL_MCP_OIDC_SIGNING_ALGORITHM") or "RS256").upper()

    @property
    def oidc_user_id_claim(self) -> str:
        return _optional_env("CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM") or "sub"

    @property
    def oidc_email_claim(self) -> str:
        return _optional_env("CBIOPORTAL_MCP_OIDC_EMAIL_CLAIM") or "email"

    @property
    def oidc_groups_claim(self) -> Optional[str]:
        """Claim path holding the caller's groups (used by acl group entries)."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM")

    @property
    def oidc_email_verified_claim(self) -> str:
        return _optional_env("CBIOPORTAL_MCP_OIDC_EMAIL_VERIFIED_CLAIM") or "email_verified"

    @property
    def oidc_trust_email_without_verified_claim(self) -> bool:
        """Trust decision for IdPs that never emit ``email_verified``.

        Off by default. When on, a token *without* the verified claim is
        accepted (the IdP is trusted to only issue verified addresses); a
        token whose claim is present but not exactly ``true`` is still refused.
        """
        return _parse_bool_env("CBIOPORTAL_MCP_OIDC_TRUST_EMAIL_WITHOUT_VERIFIED_CLAIM", False)

    @property
    def oidc_studies_claim(self) -> Optional[str]:
        """Claim path the ``jwt`` study source reads, e.g. ``realm_access.roles``."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM")

    @property
    def oidc_studies_claim_prefix(self) -> Optional[str]:
        """Only values starting with this are studies; the prefix is stripped."""
        return _optional_env("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX")

    @property
    def oidc_require_consent(self) -> bool:
        return _parse_bool_env("CBIOPORTAL_MCP_OIDC_REQUIRE_CONSENT", True)

    @property
    def oidc_allow_insecure_http_for_local_testing(self) -> bool:
        return _parse_bool_env("CBIOPORTAL_MCP_OIDC_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING", False)

    # cBioPortal Data Access Tokens (provider portal_dat).

    @property
    def portal_url(self) -> Optional[str]:
        """The portal the DAT is checked against. Deployment config only, never the client."""
        return _optional_env("CBIOPORTAL_MCP_PORTAL_URL")

    @property
    def portal_allow_insecure_http_for_local_testing(self) -> bool:
        return _parse_bool_env("CBIOPORTAL_MCP_PORTAL_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING", False)

    @property
    def portal_dat_cache_seconds(self) -> float:
        return _bounded_float_env("CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS", 30.0, 0.0, 300.0)

    @property
    def portal_timeout_seconds(self) -> float:
        return _bounded_float_env("CBIOPORTAL_MCP_PORTAL_TIMEOUT_SECONDS", 10.0, 0.5, 60.0)

    @property
    def portal_dat_hmac_secret(self) -> Optional[str]:
        """Deployment secret keying DAT cache keys and telemetry fingerprints."""
        return _optional_env("CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET")

    @property
    def portal_usernames_are_emails(self) -> bool:
        """Set only when the portal's usernames are email addresses (telemetry only)."""
        return _parse_bool_env("CBIOPORTAL_MCP_PORTAL_USERNAMES_ARE_EMAILS", False)

    @property
    def portal_confirm_show_unauthorized_studies_off(self) -> bool:
        """Operator confirmation that the portal lists only readable studies.

        With ``skin.home_page.show_unauthorized_studies=true`` newer portals
        add unreadable studies to ``GET /api/studies``; the MCP cannot see
        that property, so portal_dat refuses to start without this.
        """
        return _parse_bool_env("CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF", False)

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
