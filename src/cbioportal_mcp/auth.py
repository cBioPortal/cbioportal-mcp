"""Native MCP OAuth for cBioPortal MCP, backed directly by Google.

Opt-in via environment: unset any of the three CBIOPORTAL_MCP_GOOGLE_* variables
and the server runs exactly as it does today, unauthenticated (the existing
internal deployment LibreChat talks to). Set all three and every caller —
including direct connectors like Claude.ai, Claude Desktop, and Claude Code —
must complete a real Google login before any tool call succeeds.

Any Google account can authenticate; there is no Workspace-domain
restriction. This is deliberate: not everyone who should get MCP access has
an account in the org's existing Keycloak instance, so Keycloak (which would
otherwise be the natural reuse of existing infra) was ruled out as a hard
requirement for authentication here.

Deliberately scoped to authentication only. It says nothing about which
studies an authenticated caller can see — that's `cbioportal_mcp.authentication`,
a separate, independent piece of work.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from cbioportal_mcp.env import get_mcp_config

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


def _build_auth_provider() -> GoogleProvider | None:
    """Build the OAuth provider for this deployment, or None to stay unauthenticated.

    Requires all three CBIOPORTAL_MCP_GOOGLE_* environment variables to be
    set; with any missing, returns None (current unauthenticated behavior)
    rather than starting in a partially-configured state.
    """
    config = get_mcp_config()

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
