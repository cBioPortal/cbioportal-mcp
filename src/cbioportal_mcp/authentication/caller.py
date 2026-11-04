"""One answer to "who is calling, and what may they read" for every consumer.

``resolve_caller_identity()`` is read by authentication (the portal_dat gate),
study access (study_access.get_current_study_access) and telemetry
(telemetry._resolve_caller_identity), so they can never disagree about the
caller. What it trusts depends only on the deployment's
CBIOPORTAL_MCP_AUTH_PROVIDER:

``google`` (default)
    Exactly the pre-existing telemetry resolution: the FastMCP-validated
    Google token's ``sub``/``email``, else the LibreChat ``x-user-id`` /
    ``x-user-email`` headers. Study access keeps its existing paths (proxy
    headers for acl, the verified Google email for authorities).
``oidc``
    Only the access token FastMCP verified for this request (signature,
    issuer, audience, expiry: see cbioportal_mcp.auth.StrictJWTVerifier),
    read through configurable claim paths. No header fallback.
``portal_dat``
    The ``x-auth-token`` header, checked against the configured portal on
    every cache miss (see portal_dat.py). The owner it reports is telemetry
    only; access is the portal's own study list.

Studies and groups are returned for authorization only; telemetry never tags
them.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from cbioportal_mcp.env import (
    AuthProvider,
    McpConfig,
    StudyAccessMode,
    StudyAccessSource,
    get_mcp_config,
)

logger = logging.getLogger(__name__)

CLIENT_KIND_OAUTH = "oauth"
CLIENT_KIND_PORTAL_DAT = "portal_dat"


@dataclass(frozen=True)
class CallerIdentity:
    """The caller as every consumer sees it.

    ``studies`` is None unless studies were requested (and the source
    resolves them in the MCP); ``all_studies`` marks unrestricted access.
    """

    user_id: str | None
    email: str | None
    groups: frozenset[str]
    studies: frozenset[str] | None
    provider: str
    source: str
    client_kind: str
    all_studies: bool = False
    email_verified: bool = field(default=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "groups": self.groups,
            "studies": self.studies,
            "provider": self.provider,
            "source": self.source,
            "client_kind": self.client_kind,
        }


def effective_source(config: McpConfig) -> str:
    """The study access source in effect: ``public`` unless restricted."""
    if config.study_access_mode != StudyAccessMode.RESTRICTED.value:
        return StudyAccessMode.PUBLIC.value
    return config.study_access_source


# --- google (default): the pre-existing telemetry resolution, unchanged ------


def _extract_user_identity() -> tuple[str | None, str | None, str]:
    """Read x-user-id and x-user-email headers injected by LibreChat.

    LibreChat populates these via {{LIBRECHAT_USER_ID}} / {{LIBRECHAT_USER_EMAIL}}
    placeholders in mcpServers.headers. Returns (user_id, user_email, client) where
    client is "librechat" when the x-user-id header is present (regardless of value)
    or "direct" when the header is absent. LibreChat base64-encodes non-ASCII values
    with a "b64:" prefix.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include_all=True)
        logger.debug("_extract_user_identity: received headers: %s", sorted(headers.keys()))

        raw_id = headers.get("x-user-id", "")
        raw_email = headers.get("x-user-email", "")
        # Presence of x-user-id header (set in librechat-config.yaml mcpServers.headers)
        # distinguishes LibreChat traffic from direct MCP callers regardless of value.
        client = "librechat" if "x-user-id" in headers else "direct"

        user_id = raw_id or None
        user_email = raw_email or None
        if user_email and user_email.startswith("b64:"):
            user_email = base64.b64decode(user_email[4:]).decode("utf-8", errors="replace")
        return user_id, user_email, client
    except Exception as exc:
        logger.debug("_extract_user_identity failed: %s", exc)
        return None, None, "unknown"


def _extract_oauth_identity() -> tuple[str | None, str | None]:
    """(sub, email) claims of the access token FastMCP validated, either may be None.

    (None, None) when OAuth isn't enabled for this deployment or the caller
    has no token (stdio transport, or an unauthenticated HTTP deployment).
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is None:
            return None, None
        claims = token.claims or {}
        return claims.get("sub") or None, claims.get("email") or None
    except Exception as exc:
        logger.debug("_extract_oauth_identity failed: %s", exc)
        return None, None


def _google_caller(config: McpConfig) -> tuple[str | None, str | None, str]:
    user_id, user_email = _extract_oauth_identity()
    if user_id is not None:
        return user_id, user_email, CLIENT_KIND_OAUTH
    return _extract_user_identity()


# --- oidc: claims of the verified access token only ---------------------------


def claim_value(claims: Any, path: str | None) -> Any:
    """The value at ``path`` in ``claims``, or None.

    An exact top-level key wins (namespaced claims such as
    ``https://example.org/studies`` contain dots); otherwise ``path`` is
    walked as dot-separated object keys (``realm_access.roles``).
    """
    if not path or not isinstance(claims, dict):
        return None
    if path in claims:
        return claims[path]
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def verified_token_claims() -> dict | None:
    """Claims of the access token FastMCP validated for this request, or None."""
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return None
    claims = getattr(token, "claims", None)
    return claims if isinstance(claims, dict) else None


def _string_claim(claims: dict, path: str | None, *, max_length: int = 512) -> str | None:
    value = claim_value(claims, path)
    if isinstance(value, str) and value.strip() and len(value) <= max_length:
        return value.strip()
    return None


def _groups_claim(claims: dict, path: str | None) -> frozenset[str]:
    value = claim_value(claims, path)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())


def oidc_email_verified(claims: dict, config: McpConfig) -> bool:
    """Standard ``email_verified``: exactly ``true`` (JSON boolean).

    Absent: refused unless CBIOPORTAL_MCP_OIDC_TRUST_EMAIL_WITHOUT_VERIFIED_CLAIM
    (a documented trust decision for IdPs that never emit it). Present but
    anything other than ``true`` (``false``, ``"true"``, 1, null): refused,
    even with that setting.
    """
    path = config.oidc_email_verified_claim
    if not _claim_present(claims, path):
        return config.oidc_trust_email_without_verified_claim
    return claim_value(claims, path) is True


def _claim_present(claims: dict, path: str) -> bool:
    if path in claims:
        return True
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _oidc_caller(config: McpConfig) -> tuple[str | None, str | None, str, frozenset[str], bool]:
    try:
        claims = verified_token_claims()
    except Exception as exc:
        logger.debug("Could not read the validated access token: %s", type(exc).__name__)
        claims = None
    if claims is None:
        return None, None, "direct", frozenset(), False
    user_id = _string_claim(claims, config.oidc_user_id_claim)
    email = _string_claim(claims, config.oidc_email_claim, max_length=320)
    groups = _groups_claim(claims, config.oidc_groups_claim)
    verified = bool(email) and oidc_email_verified(claims, config)
    return user_id, email, CLIENT_KIND_OAUTH, groups, verified


def studies_from_jwt_claims(claims: dict | None, config: McpConfig) -> frozenset[str]:
    """Study IDs from the verified token's studies claim; raises when absent or malformed.

    The claim must be a JSON array of strings (a single string is accepted
    as a one-element list). With a prefix configured, only values starting
    with it count and the prefix is removed (``cbioportal:study_a`` ->
    ``study_a``). No wildcard: ``*`` (or any ID validate_study_ids rejects)
    denies the request. An absent claim denies - there is no fallback to
    ACL entries.
    """
    from cbioportal_mcp.authentication.study_access import AuthorizationError, validate_study_ids

    path = config.oidc_studies_claim
    if claims is None or not path or not _claim_present(claims, path):
        raise AuthorizationError(
            "This deployment takes study access from the login token, and the token has no "
            "study claim; sign in again or ask an administrator to grant study access."
        )
    value = claim_value(claims, path)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuthorizationError("The login token's study claim is malformed.")
    prefix = config.oidc_studies_claim_prefix
    study_ids = []
    for item in value:
        item = item.strip()
        if prefix:
            if not item.startswith(prefix):
                continue
            item = item[len(prefix) :]
        if item:
            study_ids.append(item)
    if "*" in study_ids:
        raise AuthorizationError("The login token's study claim may not contain a wildcard.")
    return validate_study_ids(study_ids)


# --- the single resolver -------------------------------------------------------


def _portal_caller(config: McpConfig, *, strict: bool):
    from cbioportal_mcp.authentication import portal_dat

    token = portal_dat.request_token(strict=strict)
    if token is None:
        return None, "direct"
    try:
        return portal_dat.lookup(token, config), CLIENT_KIND_PORTAL_DAT
    except Exception:
        if strict:
            raise
        return None, CLIENT_KIND_PORTAL_DAT


def resolve_caller_identity(
    config: McpConfig | None = None,
    *,
    include_studies: bool = False,
    strict: bool = False,
) -> CallerIdentity:
    """Who is calling, under this deployment's provider.

    ``strict=False`` (telemetry) never raises: anything unreadable yields an
    identity without user_id. ``strict=True`` (authentication and study
    access) raises AuthorizationError when the caller can't be identified.
    ``include_studies`` also resolves the caller's studies (raising
    AuthorizationError when they can't be determined; implies strict).
    """
    config = config or get_mcp_config()
    provider = config.auth_provider
    source = effective_source(config)
    strict = strict or include_studies

    if provider == AuthProvider.PORTAL_DAT.value:
        lookup, client_kind = _portal_caller(config, strict=strict)
        if lookup is None:
            if strict:
                from cbioportal_mcp.authentication.study_access import AuthorizationError

                raise AuthorizationError(
                    "This cBioPortal MCP deployment requires a cBioPortal Data Access Token "
                    "in the x-auth-token header."
                )
            return CallerIdentity(None, None, frozenset(), None, provider, source, client_kind)
        return CallerIdentity(
            user_id=lookup.telemetry_user_id,
            email=lookup.telemetry_email,
            groups=frozenset(),
            studies=lookup.studies if include_studies else None,
            provider=provider,
            source=source,
            client_kind=client_kind,
        )

    if provider == AuthProvider.OIDC.value:
        user_id, email, client_kind, groups, verified = _oidc_caller(config)
        if strict and user_id is None:
            from cbioportal_mcp.authentication.study_access import AuthorizationError

            raise AuthorizationError(
                "This cBioPortal MCP deployment requires a signed-in identity; none was found."
            )
        studies = None
        if include_studies and source == StudyAccessSource.JWT.value:
            studies = studies_from_jwt_claims(verified_token_claims(), config)
        identity = CallerIdentity(
            user_id=user_id,
            email=email,
            groups=groups,
            studies=studies,
            provider=provider,
            source=source,
            client_kind=client_kind,
            email_verified=verified,
        )
        if include_studies and studies is None:
            return _with_study_access(identity, config)
        return identity

    user_id, email, client_kind = _google_caller(config)
    identity = CallerIdentity(
        user_id=user_id,
        email=email,
        groups=frozenset(),
        studies=None,
        provider=provider,
        source=source,
        client_kind=client_kind,
    )
    if include_studies:
        return _with_study_access(identity, config)
    return identity


def _with_study_access(identity: CallerIdentity, config: McpConfig) -> CallerIdentity:
    """Fill studies from study_access for the sources it resolves itself."""
    from dataclasses import replace

    from cbioportal_mcp.authentication.study_access import get_current_study_access

    access = get_current_study_access(config)
    return replace(
        identity,
        groups=access.identity.groups or identity.groups,
        studies=access.studies,
        all_studies=access.all_studies,
    )
