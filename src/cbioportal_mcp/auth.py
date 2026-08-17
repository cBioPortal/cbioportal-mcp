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
from collections.abc import Mapping, Sequence
from typing import Any, SupportsFloat

from cryptography.fernet import Fernet
from fastmcp import settings as fastmcp_settings
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.base import BaseWrapper
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from cbioportal_mcp.env import get_mcp_config

logger = logging.getLogger(__name__)

# Collections FastMCP's OAuthProxy always uses exclusively for refresh-token
# state (oauth_proxy.py:883-931) -- safe to extend their TTL unconditionally.
_REFRESH_SCOPED_COLLECTIONS = frozenset({"mcp-refresh-tokens", "mcp-upstream-tokens"})

# "mcp-jti-mappings" holds BOTH short-lived access-token JTI mappings
# (~expires_in, ~1 hour) and long-lived refresh-token JTI mappings (~30-day
# fallback) in the same collection -- distinguished here by TTL magnitude
# rather than a hardcoded literal match, since the two are orders of
# magnitude apart regardless of the exact fallback constant FastMCP uses.
_MIXED_TTL_COLLECTION = "mcp-jti-mappings"
_ACCESS_TOKEN_TTL_CEILING_SECONDS = 60 * 60 * 24  # 1 day


class _RefreshTTLFloorWrapper(BaseWrapper):
    """Extends OAuthProxy's refresh-token storage TTL past its hardcoded
    30-day fallback (oauth_proxy.py, six inlined occurrences of
    `60 * 60 * 24 * 30`), used whenever the upstream identity provider's
    token response omits `refresh_expires_in` — true for Google, unlike e.g.
    Keycloak. Google's real production refresh tokens don't expire on a
    fixed calendar schedule; the 30-day figure is FastMCP's own conservative
    local guess, not a real upstream limit — and it's a hard cliff from
    first login, not a sliding window (confirmed by reading
    `exchange_refresh_token`): an active daily user hits it exactly as fast
    as an idle one.

    Deliberately doesn't touch OAuthProxy's token-exchange logic at all —
    that's ~200 lines of core OAuth security code with the fallback inlined
    directly into it, no factored-out helper to override cleanly. Wrapping
    the storage layer instead means this survives FastMCP upgrades as long
    as the AsyncKeyValue protocol itself doesn't change.
    """

    def __init__(self, key_value: AsyncKeyValue, ttl_seconds: float) -> None:
        self.key_value = key_value
        self._ttl_seconds = ttl_seconds

    def _extended_ttl(
        self, collection: str | None, ttl: SupportsFloat | None
    ) -> SupportsFloat | None:
        if collection in _REFRESH_SCOPED_COLLECTIONS:
            return self._ttl_seconds
        if (
            collection == _MIXED_TTL_COLLECTION
            and ttl is not None
            and float(ttl) > _ACCESS_TOKEN_TTL_CEILING_SECONDS
        ):
            return self._ttl_seconds
        return ttl

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        return await self.key_value.put(
            key=key,
            value=value,
            collection=collection,
            ttl=self._extended_ttl(collection, ttl),
        )

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        return await self.key_value.put_many(
            keys=keys,
            values=values,
            collection=collection,
            ttl=self._extended_ttl(collection, ttl),
        )


def _build_client_storage(
    client_secret: str, ttl_days: int
) -> tuple[bytes, AsyncKeyValue]:
    """Replicate FastMCP's default encrypted-disk-store construction
    (oauth_proxy.py's own fallback when client_storage=None, oauth_proxy.py:858-867)
    so tokens written by our wrapped storage can still be decrypted exactly
    the way FastMCP itself would, then layer the TTL-extending wrapper on top.

    Returns (jwt_signing_key, client_storage). jwt_signing_key must be passed
    to GoogleProvider explicitly (rather than left to auto-derive) so both
    pieces agree on the same key — OAuthProxy's internal token stores bind
    to whatever client_storage was at __init__ time, so there's no way to
    swap storage in after construction and have it take effect.
    """
    jwt_signing_key = derive_jwt_key(
        high_entropy_material=client_secret, salt="fastmcp-jwt-signing-key"
    )
    storage_encryption_key = derive_jwt_key(
        high_entropy_material=jwt_signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )
    base_store = FernetEncryptionWrapper(
        key_value=DiskStore(directory=fastmcp_settings.home / "oauth-proxy"),
        fernet=Fernet(key=storage_encryption_key),
    )
    client_storage = _RefreshTTLFloorWrapper(
        key_value=base_store, ttl_seconds=ttl_days * 60 * 60 * 24
    )
    return jwt_signing_key, client_storage


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

    jwt_signing_key, client_storage = _build_client_storage(
        client_secret, config.google_refresh_ttl_days
    )

    logger.info("✅ Google OAuth enabled for client %s", client_id)
    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        # Explicit (rather than left to auto-derive) so _build_client_storage
        # above can independently derive the matching storage-encryption key —
        # see its docstring for why that has to happen at construction time.
        jwt_signing_key=jwt_signing_key,
        # Extends refresh-session storage past OAuthProxy's hardcoded 30-day
        # fallback — see _RefreshTTLFloorWrapper's docstring.
        client_storage=client_storage,
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
