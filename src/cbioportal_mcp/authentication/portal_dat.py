"""Study access from a pasted cBioPortal Data Access Token (provider portal_dat).

For deployments whose portal logs users in with SAML: the MCP cannot do that
login itself, so the user pastes a portal Data Access Token (DAT) and the
client sends it in the ``x-auth-token`` header. Nothing on the portal side
changes - the MCP only makes these reads, against the portal configured in
CBIOPORTAL_MCP_PORTAL_URL (never a URL from the client):

``GET /api/studies?projection=SUMMARY`` with ``Authorization: Bearer <DAT>``
    The allowed studies are exactly the returned list. At cBioPortal 5677496
    (v7.0.0+5) - and in every tag from v7.0.0 to v7.2.4 - this route is
    ColumnStoreStudyController.getAllStudies, whose use case carries
    ``@PostFilter(hasPermission(filterObject, READ))``. Since cBioPortal
    aa14324c47 (in v7.0.6, not in v7.0.1) the filter also admits LIST when
    skin.home_page.show_unauthorized_studies is on, so the deployment must confirm it is off
    (CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF). Minimum
    portal: v7.0.0. ``readPermission`` in the response is never consulted
    (at 5677496 it is just ``publicStudy``).
``GET /api/studies?projection=SUMMARY&pageSize=1`` without credentials
    Must be refused (401/403). A portal that answers it (``authenticate=false``,
    or ``optional_oauth2``) serves /api without authentication and without
    the READ filter, so every DAT is denied.
``GET /api/data-access-tokens`` with the DAT in the Authorization header
    Telemetry only: the owner's ``username`` (and the matching token's
    expiration, to bound the cache). The response lists the owner's token
    secrets; everything else is dropped immediately. Never
    ``GET /api/data-access-tokens/{token}`` (the secret would be in the URL),
    never POST or DELETE.

Every failure denies: portal errors, timeouts, redirects, non-JSON or
malformed bodies, invalid study IDs. The raw token never appears in logs,
exception messages, telemetry or cache keys: cache keys and fingerprints are
HMAC-SHA256 under CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from cbioportal_mcp.authentication.study_access import AuthorizationError, validate_study_ids
from cbioportal_mcp.env import McpConfig, get_mcp_config

logger = logging.getLogger(__name__)

DAT_HEADER = "x-auth-token"
_MAX_TOKEN_LENGTH = 4096
# UUIDs, JWTs (base64url segments) and other opaque tokens; no whitespace or
# control characters, so the value can't smuggle extra header lines.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]+")
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_CACHE_ENTRIES = 4096
_PROBE_OK_SECONDS = 300.0
_MAX_USERNAME_LENGTH = 320

# Test hook: an httpx transport used instead of the network (unit tests only).
_transport_for_tests: httpx.BaseTransport | None = None


class PortalDatError(AuthorizationError):
    """A DAT could not be verified. Messages never contain the token."""


@dataclass(frozen=True)
class DatLookup:
    """What one DAT resolved to. Holds no part of the token."""

    studies: frozenset[str]
    telemetry_user_id: str
    telemetry_email: str | None
    owner_source: str  # "username" | "sub_hint" | "fingerprint"


_NO_TOKEN_MESSAGE = (
    "This cBioPortal MCP deployment requires a cBioPortal Data Access Token in the "
    f"{DAT_HEADER} header."
)


def request_token(*, strict: bool = True) -> str | None:
    """The DAT from this request's x-auth-token header, or None when absent.

    A value that isn't token-shaped raises PortalDatError when ``strict``
    (and counts as absent otherwise) without being echoed anywhere.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include_all=True)
    except Exception:
        return None
    raw = None
    for name, value in headers.items():
        if str(name).lower() == DAT_HEADER:
            raw = value
            break
    if raw is None:
        return None
    token = str(raw).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return None
    if len(token) > _MAX_TOKEN_LENGTH or not _TOKEN_SHAPE.fullmatch(token):
        if strict:
            raise PortalDatError("The data access token in the x-auth-token header is malformed.")
        return None
    return token


# --- configuration checks ----------------------------------------------------


def portal_base_url(config: McpConfig) -> str:
    """The validated portal URL (no trailing slash); raises ValueError if unsafe."""
    raw = config.portal_url
    if not raw:
        raise ValueError("CBIOPORTAL_MCP_PORTAL_URL is required for portal_dat.")
    parsed = urlparse(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("CBIOPORTAL_MCP_PORTAL_URL must be an absolute https:// URL.")
    if parsed.scheme != "https" and not config.portal_allow_insecure_http_for_local_testing:
        raise ValueError(
            "CBIOPORTAL_MCP_PORTAL_URL must use https (a DAT is a full portal credential); "
            "CBIOPORTAL_MCP_PORTAL_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING=true allows http for "
            "local tests only."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "CBIOPORTAL_MCP_PORTAL_URL may not contain credentials, a query or a fragment."
        )
    return raw.rstrip("/")


def portal_namespace(config: McpConfig) -> str:
    """Telemetry namespace for DAT owners: the portal's host[:port]."""
    parsed = urlparse(portal_base_url(config))
    return parsed.netloc.lower()


def _hmac_secret(config: McpConfig) -> bytes:
    secret = config.portal_dat_hmac_secret
    if not secret or len(secret) < 32:
        raise PortalDatError("This deployment's DAT verification is not configured.")
    return secret.encode("utf-8")


def _keyed_digest(config: McpConfig, purpose: str, token: str) -> str:
    return hmac.new(
        _hmac_secret(config), f"{purpose}\0{token}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


# --- cache -------------------------------------------------------------------


class _LookupCache:
    """Per-caller (per-token) results, keyed by portal + HMAC of the token."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[float, DatLookup]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str]) -> DatLookup | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: tuple[str, str], value: DatLookup, expires_at: float) -> None:
        with self._lock:
            self._entries[key] = (expires_at, value)
            self._entries.move_to_end(key)
            while len(self._entries) > _MAX_CACHE_ENTRIES:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._entries)


_cache = _LookupCache()
_probe_ok_until: dict[str, float] = {}
_probe_lock = threading.Lock()


def reset_for_tests() -> None:
    _cache.clear()
    with _probe_lock:
        _probe_ok_until.clear()


# --- portal reads --------------------------------------------------------------


def _client(config: McpConfig) -> httpx.Client:
    return httpx.Client(
        base_url=portal_base_url(config),
        timeout=config.portal_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        transport=_transport_for_tests,
        headers={"accept": "application/json"},
    )


def _get(client: httpx.Client, path: str, *, token: str | None, params=None) -> httpx.Response:
    """One GET. Transport errors become a sanitized PortalDatError."""
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    try:
        return client.get(path, params=params, headers=headers)
    except httpx.TimeoutException:
        logger.warning("cBioPortal %s timed out", path)
        raise PortalDatError("cBioPortal did not answer in time; access denied.") from None
    except httpx.HTTPError as exc:
        logger.warning("cBioPortal %s failed: %s", path, type(exc).__name__)
        raise PortalDatError("cBioPortal could not be reached; access denied.") from None


def _json_body(response: httpx.Response, what: str):
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise PortalDatError(f"cBioPortal's {what} response was too large; access denied.")
    try:
        return json.loads(response.content)
    except ValueError:
        raise PortalDatError(f"cBioPortal's {what} response was not JSON; access denied.") from None


def _check_portal_requires_auth(client: httpx.Client, base_url: str) -> None:
    """Deny unless the portal refuses an anonymous study list (cached ~5 min)."""
    now = time.monotonic()
    with _probe_lock:
        if _probe_ok_until.get(base_url, 0.0) > now:
            return
    response = _get(
        client, "/api/studies", token=None, params={"projection": "SUMMARY", "pageSize": "1"}
    )
    if response.status_code in (401, 403):
        with _probe_lock:
            _probe_ok_until[base_url] = now + _PROBE_OK_SECONDS
        return
    if 200 <= response.status_code < 300:
        logger.error(
            "cBioPortal answers GET /api/studies without credentials: it does not enforce "
            "authentication (or study READ filtering) on /api, so DATs are refused."
        )
        raise PortalDatError(
            "The configured cBioPortal serves its API without authentication, so it cannot "
            "vouch for a data access token; access denied."
        )
    logger.warning("cBioPortal anonymous probe returned HTTP %s", response.status_code)
    raise PortalDatError("cBioPortal could not confirm it enforces authentication; access denied.")


def _fetch_studies(client: httpx.Client, token: str) -> frozenset[str]:
    response = _get(client, "/api/studies", token=token, params={"projection": "SUMMARY"})
    if response.status_code in (401, 403):
        raise PortalDatError("cBioPortal rejected the data access token (expired or revoked?).")
    if response.status_code != 200:
        logger.warning("cBioPortal GET /api/studies returned HTTP %s", response.status_code)
        raise PortalDatError("cBioPortal could not list the token's studies; access denied.")
    body = _json_body(response, "study list")
    if not isinstance(body, list):
        raise PortalDatError("cBioPortal's study list was malformed; access denied.")
    study_ids = []
    for item in body:
        study_id = item.get("studyId") if isinstance(item, dict) else None
        if not isinstance(study_id, str) or not study_id:
            raise PortalDatError("cBioPortal's study list was malformed; access denied.")
        study_ids.append(study_id)
    if "*" in study_ids:
        raise PortalDatError("cBioPortal's study list was malformed; access denied.")
    try:
        return validate_study_ids(study_ids)
    except AuthorizationError:
        raise PortalDatError("cBioPortal returned an invalid study identifier.") from None


def _parse_expiration(value) -> float | None:
    """Epoch seconds of a DataAccessToken ``expiration`` (Jackson, UTC by default)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0 if value > 10**11 else float(value)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(value, fmt)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return None


def _owner_from_token_list(client: httpx.Client, token: str) -> tuple[str | None, float | None]:
    """(username, this token's expiry) from GET /api/data-access-tokens, or (None, None).

    The body lists every token of the owner, secrets included: only the
    username and the matching entry's expiration are kept, and the parsed
    body is dropped before returning.
    """
    try:
        response = _get(client, "/api/data-access-tokens", token=token)
    except PortalDatError:
        return None, None
    if response.status_code != 200:
        # download_group denies, dat.method=oauth2 answers "unsupported".
        logger.info("cBioPortal token-owner lookup unavailable (HTTP %s)", response.status_code)
        return None, None
    username: str | None = None
    expires_at: float | None = None
    try:
        body = _json_body(response, "token list")
    except PortalDatError:
        return None, None
    try:
        if isinstance(body, list):
            for item in body:
                if not isinstance(item, dict):
                    continue
                name = item.get("username")
                if username is None and isinstance(name, str) and name.strip():
                    username = name.strip()[:_MAX_USERNAME_LENGTH]
                listed = item.get("token")
                if isinstance(listed, str) and hmac.compare_digest(
                    listed.encode("utf-8"), token.encode("utf-8")
                ):
                    expires_at = _parse_expiration(item.get("expiration"))
    finally:
        del body
        del response
    return username, expires_at


def _unverified_jwt_payload(token: str) -> dict | None:
    """Payload of a JWT-shaped token, unverified (telemetry hint / cache bound only)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _telemetry_owner(config: McpConfig, token: str, username: str | None):
    """(enduser.id, enduser.email, how it was derived). Never used for access."""
    namespace = portal_namespace(config)
    if username:
        email = None
        if config.portal_usernames_are_emails and re.fullmatch(r"[^@\s]+@[^@\s]+", username):
            email = username.lower()
        return f"portal:{namespace}:{username}", email, "username"
    payload = _unverified_jwt_payload(token)
    sub = payload.get("sub") if payload else None
    if isinstance(sub, str) and sub.strip() and len(sub) <= 256:
        # Not verified by the MCP; the portal accepted the token, the sub is a hint.
        return f"portal:{namespace}:sub:{sub.strip()}", None, "sub_hint"
    fingerprint = _keyed_digest(config, "fingerprint", token)[:24]
    return f"portal:{namespace}:fp:{fingerprint}", None, "fingerprint"


def lookup(token: str, config: McpConfig | None = None) -> DatLookup:
    """Resolve a DAT to the portal's study list (cached briefly, per token)."""
    config = config or get_mcp_config()
    try:
        base_url = portal_base_url(config)
    except ValueError:
        raise PortalDatError("This deployment's DAT verification is not configured.") from None
    key = (base_url, _keyed_digest(config, "cache", token))
    cached = _cache.get(key)
    if cached is not None:
        return cached

    with _client(config) as client:
        _check_portal_requires_auth(client, base_url)
        studies = _fetch_studies(client, token)
        username, expires_at = _owner_from_token_list(client, token)

    if expires_at is None:
        payload = _unverified_jwt_payload(token)
        exp = payload.get("exp") if payload else None
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            expires_at = float(exp)
    user_id, email, owner_source = _telemetry_owner(config, token, username)
    result = DatLookup(
        studies=studies,
        telemetry_user_id=user_id,
        telemetry_email=email,
        owner_source=owner_source,
    )

    ttl = config.portal_dat_cache_seconds
    if expires_at is not None:
        ttl = min(ttl, expires_at - time.time())
    if ttl > 0:
        _cache.put(key, result, time.monotonic() + ttl)
    return result
