"""Native MCP login for cBioPortal MCP, chosen per deployment.

CBIOPORTAL_MCP_AUTH_PROVIDER selects one provider:

``google`` (default)
    Opt-in via environment: unset any of the three CBIOPORTAL_MCP_GOOGLE_*
    variables and the server runs exactly as it does today, unauthenticated
    (the existing internal deployment LibreChat talks to). Set all three and
    every caller must complete a real Google login before any tool call
    succeeds. Any Google account can authenticate; there is no
    Workspace-domain restriction. This is the public deployment, with study
    access off.
``oidc``
    Generic, standards-based OIDC (discovery + JWKS) against whatever IdP the
    deployment already has - Keycloak, Okta, Entra ID, ... - purely by
    configuration. Access tokens are verified by StrictJWTVerifier.
``portal_dat``
    For SAML portals: the user pastes a cBioPortal Data Access Token, checked
    against the portal on every cache miss (authentication/portal_dat.py).

Which studies a caller can see is `cbioportal_mcp.authentication`.
"""

from __future__ import annotations

import functools
import logging
import time

from cryptography.fernet import Fernet
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.middleware import Middleware
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
from pydantic import AnyHttpUrl

from cbioportal_mcp.env import AuthProvider, get_mcp_config

logger = logging.getLogger(__name__)


def _derive_storage_encryption_key(client_secret: str) -> bytes:
    """Derive the Fernet key used to encrypt OAuth storage at rest.

    Mirrors, step for step, the derivation `OAuthProxy.__init__` uses to
    build its own default disk store's encryption key (same two-step HKDF
    chain, same salts) — see fastmcp's `oauth_proxy` module. Reusing it here
    means we don't have to pass a `jwt_signing_key` override to
    `GoogleProvider`: it independently derives the same signing key from
    `client_secret` on every boot, so this stays in sync without the two
    being wired together explicitly. Deterministic from `client_secret`
    alone, so the key is stable across restarts — required, since Redis is
    exactly the place values now persist across them.
    """
    jwt_signing_key = derive_jwt_key(
        high_entropy_material=client_secret,
        salt="fastmcp-jwt-signing-key",
    )
    return derive_jwt_key(
        high_entropy_material=jwt_signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )


def _build_client_storage(client_secret: str, redis_url: str) -> AsyncKeyValue:
    """Encrypted Redis-backed store for OAuth client registrations and tokens.

    Redis survives pod restarts; FastMCP's default local-disk store does
    not — it lives on the pod's own ephemeral filesystem, so every restart
    (a new image via Keel, a ConfigMap-triggered reload, ...) silently
    invalidates every client registration and token, kicking users off with
    an `invalid_token` 401 until they clear local state and re-register.
    """
    return FernetEncryptionWrapper(
        key_value=RedisStore(url=redis_url, default_collection="cbioportal-mcp-oauth"),
        fernet=Fernet(key=_derive_storage_encryption_key(client_secret)),
    )


class StrictJWTVerifier(JWTVerifier):
    """FastMCP's JWTVerifier, but every check is mandatory.

    JWTVerifier verifies the signature (pinned to one asymmetric algorithm,
    key from the issuer's JWKS) and checks ``exp``/``iss``/``aud`` only when
    they are present or configured. Here a token is accepted only if it has a
    numeric ``exp`` in the future, ``iss`` exactly equal to the configured
    issuer, an ``aud`` containing one of the configured audiences, and no
    ``nbf`` in the future. Anything else is rejected (None -> HTTP 401).
    """

    _CLOCK_SKEW_SECONDS = 60

    def __init__(self, *, jwks_uri: str, issuer: str, audience: list[str], algorithm: str, **kw):
        if not issuer or not audience:
            raise ValueError("StrictJWTVerifier requires an issuer and an audience")
        super().__init__(
            jwks_uri=jwks_uri, issuer=issuer, audience=audience, algorithm=algorithm, **kw
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = access.claims if isinstance(access.claims, dict) else {}
        now = time.time()
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= now:
            logger.info("OIDC token rejected: missing or expired exp")
            return None
        if claims.get("iss") != self.issuer:
            logger.info("OIDC token rejected: issuer mismatch")
            return None
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if not any(isinstance(a, str) and a in self.audience for a in auds):
            logger.info("OIDC token rejected: audience mismatch")
            return None
        nbf = claims.get("nbf")
        if nbf is not None and (
            isinstance(nbf, bool)
            or not isinstance(nbf, (int, float))
            or nbf > now + self._CLOCK_SKEW_SECONDS
        ):
            logger.info("OIDC token rejected: not yet valid")
            return None
        return access


def _oidc_client_storage(client_secret: str, redis_url: str) -> AsyncKeyValue:
    """Encrypted Redis store for the OIDC provider, in its own key namespace.

    FastMCP names its collections itself (mcp-oauth-proxy-clients, ...), so
    without a prefix two providers sharing one Redis database would share
    keys. Prefixing only the new provider keeps every Google key and path
    exactly as before, and switching provider and back leaves both sets of
    sessions readable.
    """
    return PrefixCollectionsWrapper(
        key_value=FernetEncryptionWrapper(
            key_value=RedisStore(url=redis_url, default_collection="cbioportal-mcp-oauth-oidc"),
            fernet=Fernet(key=_derive_storage_encryption_key(client_secret)),
        ),
        prefix=OIDC_STORAGE_PREFIX,
    )


OIDC_STORAGE_PREFIX = "cbioportal-mcp-oidc"


def _build_oidc_provider(config) -> OIDCProxy:
    """Generic OIDC login: discovery + JWKS from the configured issuer.

    Nothing here is specific to one IdP; Keycloak, Okta, Entra ID and others
    differ only in configuration (issuer, audience, claim paths).
    """
    from cbioportal_mcp.authentication.study_access import validate_study_access_startup_config

    # Refuse to build anything from an incomplete/unsafe configuration.
    validate_study_access_startup_config(config)

    discovery = OIDCConfiguration.get_oidc_configuration(
        AnyHttpUrl(config.oidc_config_url), strict=True, timeout_seconds=15
    )
    if str(discovery.issuer).rstrip("/") != config.oidc_issuer_url.rstrip("/"):
        raise ValueError(
            "The OIDC discovery document's issuer does not match CBIOPORTAL_MCP_OIDC_ISSUER_URL."
        )
    verifier = StrictJWTVerifier(
        jwks_uri=str(discovery.jwks_uri),
        # Compared verbatim with each token's iss (pydantic may normalize the
        # discovery copy, e.g. add a trailing slash).
        issuer=config.oidc_issuer_url,
        audience=config.oidc_audiences,
        algorithm=config.oidc_signing_algorithm,
        required_scopes=config.oidc_scopes,
    )
    storage_kwargs = {}
    if config.redis_url:
        storage_kwargs["client_storage"] = _oidc_client_storage(
            config.oidc_client_secret, config.redis_url
        )
    else:
        logger.warning(
            "REDIS_URL not set — OIDC client registrations and tokens will be stored on "
            "local disk, which is lost on every pod restart."
        )
    logger.info("✅ OIDC login enabled (issuer %s)", config.oidc_issuer_url)
    return OIDCProxy(
        config_url=config.oidc_config_url,
        strict=True,
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        base_url=config.oidc_base_url,
        token_verifier=verifier,
        require_authorization_consent=config.oidc_require_consent,
        timeout_seconds=15,
        **storage_kwargs,
    )


def _build_auth_provider():
    """Build the login provider for this deployment, or None to stay unauthenticated.

    CBIOPORTAL_MCP_AUTH_PROVIDER picks it (default ``google``):

    - ``google``: unchanged - all three CBIOPORTAL_MCP_GOOGLE_* set enables it;
      with any missing, returns None (unauthenticated) rather than starting
      in a partially-configured state.
    - ``oidc``: OIDCProxy against the configured issuer; refuses to start on
      incomplete configuration.
    - ``portal_dat``: no HTTP-level provider; each request's x-auth-token is
      checked against the portal (see authentication/portal_dat.py and
      PortalDatAuthMiddleware).
    """
    config = get_mcp_config()
    provider = config.auth_provider
    if provider == AuthProvider.OIDC.value:
        return _build_oidc_provider(config)
    if provider == AuthProvider.PORTAL_DAT.value:
        logger.info("✅ cBioPortal Data Access Token login enabled (%s)", config.portal_url)
        return None

    client_id = config.google_client_id
    client_secret = config.google_client_secret
    base_url = config.google_base_url

    if not (client_id and client_secret and base_url):
        missing = [
            name
            for name, value in (
                ("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", client_id),
                ("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET", client_secret),
                ("CBIOPORTAL_MCP_GOOGLE_BASE_URL", base_url),
            )
            if not value
        ]
        if 0 < len(missing) < 3:
            logger.warning(
                "OAuth partially configured (missing %s) — running unauthenticated.",
                ", ".join(missing),
            )
        return None

    logger.info("✅ Google OAuth enabled for client %s", client_id)

    redis_url = config.redis_url
    if redis_url:
        client_storage_kwargs = {"client_storage": _build_client_storage(client_secret, redis_url)}
    else:
        client_storage_kwargs = {}
        logger.warning(
            "REDIS_URL not set — OAuth client registrations and tokens will "
            "be stored on local disk, which is lost on every pod restart."
        )

    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        **client_storage_kwargs,
        # Request `openid`, `email`, AND `profile` so `enduser.email`
        # populates on spans. Empirically, FastMCP's GoogleTokenVerifier
        # hits Google's legacy /oauth2/v2/userinfo endpoint which only
        # returns the `email` field when the token also carries `profile`
        # scope — even though the token was granted `email`. Requesting
        # `email` alone leaves telemetry.py's `_extract_oauth_identity`
        # with only `sub` (opaque Google account ID), which can't be
        # mapped back to a person without a live OAuth session for that
        # user. Neither `email` nor `profile` is a sensitive Google scope;
        # no verification cycle required, adds ~one line each to the
        # consent screen.
        required_scopes=["openid", "email", "profile"],
        # Skips FastMCP's own consent interstitial so the flow goes straight
        # to Google's real login page — a temporary UX call, not a security
        # one: FastMCP's docs flag this as normally only for local dev, since
        # the consent screen is what protects against a malicious MCP client
        # silently getting authorized without the user seeing which app is
        # asking for access ("confused deputy" problem). Revisit before wide
        # rollout — the fix there is a custom-branded consent page, not this.
        require_authorization_consent=False,
    )


class PortalDatAuthMiddleware(Middleware):
    """Require a portal-verified Data Access Token for every tool, resource and prompt.

    Discovery (tools/list, ...) stays open so clients can connect; anything
    that returns content needs an x-auth-token the configured portal accepts
    (see authentication/portal_dat.py). Added after TelemetryMiddleware, so
    refused calls are still traced.
    """

    async def _require(self) -> None:
        import anyio

        from cbioportal_mcp.authentication.caller import resolve_caller_identity
        from cbioportal_mcp.authentication.study_access import AuthorizationError

        try:
            await anyio.to_thread.run_sync(functools.partial(resolve_caller_identity, strict=True))
        except AuthorizationError as exc:
            raise ToolError(str(exc)) from None

    async def on_call_tool(self, context, call_next):
        await self._require()
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        await self._require()
        return await call_next(context)

    async def on_get_prompt(self, context, call_next):
        await self._require()
        return await call_next(context)
