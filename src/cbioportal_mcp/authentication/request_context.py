"""Request-local study authorization extraction for MCP HTTP transports."""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Generator, Mapping

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from starlette.responses import JSONResponse

from cbioportal_mcp.authentication.study_scope import StudyScope, parse_study_scope_from_claims
from cbioportal_mcp.env import McpConfig, get_mcp_config


class StudyAuthError(Exception):
    """Raised when request identity cannot be converted into a study scope."""


_current_study_scope: ContextVar[StudyScope | None] = ContextVar(
    "current_study_scope",
    default=None,
)
_jwks_cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
_JWKS_CACHE_TTL_SECONDS = 300


@contextmanager
def set_current_study_scope(scope: StudyScope) -> Generator[StudyScope, None, None]:
    """Set the current request-local study scope."""
    token: Token = _current_study_scope.set(scope)
    try:
        yield scope
    finally:
        _current_study_scope.reset(token)


def get_current_study_scope(config: McpConfig | None = None) -> StudyScope:
    """Return the current request's study scope.

    HTTP/SSE requests derive scope from the Authorization header. Stdio and
    other non-HTTP contexts have no headers, so they fall back to claim parsing
    with no claims. Later query-execution PRs will call this helper.
    """
    scope = _current_study_scope.get()
    if scope is not None:
        return scope

    config = config or get_mcp_config()
    if not _has_http_context():
        return parse_study_scope_from_claims(None, config)

    return _study_scope_from_http_headers(_get_http_headers(), config)


def study_scope_from_authorization_header(
    authorization: str | None,
    config: McpConfig,
) -> StudyScope:
    """Parse a Bearer token header into a StudyScope."""
    if not config.cbioportal_auth_enabled:
        return parse_study_scope_from_claims(None, config)

    if not authorization:
        if config.cbioportal_auth_required:
            raise StudyAuthError("Missing Authorization bearer token.")
        return parse_study_scope_from_claims(None, config)

    token = _extract_bearer_token(authorization)
    if not token:
        if config.cbioportal_auth_required:
            raise StudyAuthError("Malformed Authorization bearer token.")
        return parse_study_scope_from_claims(None, config)

    claims = decode_jwt_claims(token, config)
    return parse_study_scope_from_claims(claims, config)


def decode_jwt_claims(token: str, config: McpConfig) -> Mapping[str, Any]:
    """Decode JWT claims using verified mode or explicit dev-only unverified mode."""
    if config.cbioportal_auth_allow_unverified_jwt_for_dev:
        claims = _decode_unverified_jwt_claims(token)
    else:
        claims = _decode_verified_jwt_claims(token, config)

    _validate_standard_claims(claims, config)
    return claims


class StudyAuthMiddleware:
    """Starlette middleware that computes request-local study scope for HTTP MCP."""

    def __init__(self, app, config: McpConfig | None = None):
        self.app = app
        self.config = config or get_mcp_config()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.config.cbioportal_auth_enabled:
            await self.app(scope, receive, send)
            return

        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        try:
            study_scope = study_scope_from_authorization_header(
                headers.get("authorization"),
                self.config,
            )
        except StudyAuthError as exc:
            if self.config.cbioportal_auth_required:
                response = JSONResponse({"error": str(exc)}, status_code=401)
                await response(scope, receive, send)
                return
            study_scope = parse_study_scope_from_claims(None, self.config)

        with set_current_study_scope(study_scope):
            await self.app(scope, receive, send)


def _study_scope_from_http_headers(headers: Mapping[str, str], config: McpConfig) -> StudyScope:
    return study_scope_from_authorization_header(headers.get("authorization"), config)


def _extract_bearer_token(authorization: str) -> str | None:
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


def _decode_verified_jwt_claims(token: str, config: McpConfig) -> Mapping[str, Any]:
    key = _jwt_verification_key(token, config)
    jwt = JsonWebToken([config.cbioportal_auth_jwt_algorithm])
    try:
        claims = jwt.decode(token, key)
        claims.validate()
        return dict(claims)
    except JoseError as exc:
        raise StudyAuthError("JWT verification failed.") from exc
    except Exception as exc:
        raise StudyAuthError("JWT validation failed.") from exc


def _jwt_verification_key(token: str, config: McpConfig) -> str | Any:
    if config.cbioportal_auth_public_key:
        return config.cbioportal_auth_public_key
    if config.cbioportal_auth_jwks_uri:
        return _jwks_key_for_token(token, config.cbioportal_auth_jwks_uri)
    raise StudyAuthError(
        "JWT verification is not configured. Set CBIOPORTAL_AUTH_JWKS_URI or "
        "CBIOPORTAL_AUTH_PUBLIC_KEY, or enable "
        "CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV for local development only."
    )


def _jwks_key_for_token(token: str, jwks_uri: str) -> Any:
    header = _decode_jwt_part(token, index=0)
    token_kid = header.get("kid")
    jwks = _fetch_jwks(jwks_uri)
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise StudyAuthError("JWKS did not contain signing keys.")

    if token_kid:
        for key_data in keys:
            if isinstance(key_data, Mapping) and key_data.get("kid") == token_kid:
                return JsonWebKey.import_key(key_data)
        raise StudyAuthError("No JWKS key matched the JWT key ID.")

    if len(keys) == 1 and isinstance(keys[0], Mapping):
        return JsonWebKey.import_key(keys[0])

    raise StudyAuthError("JWT key ID is required when JWKS contains multiple keys.")


def _fetch_jwks(jwks_uri: str) -> Mapping[str, Any]:
    cached = _jwks_cache.get(jwks_uri)
    if cached and time.time() - cached[0] < _JWKS_CACHE_TTL_SECONDS:
        return cached[1]

    with urllib.request.urlopen(jwks_uri, timeout=5) as response:
        jwks = json.loads(response.read().decode("utf-8"))
    if not isinstance(jwks, Mapping):
        raise StudyAuthError("JWKS response was not a JSON object.")
    _jwks_cache[jwks_uri] = (time.time(), jwks)
    return jwks


def _decode_unverified_jwt_claims(token: str) -> Mapping[str, Any]:
    return _decode_jwt_part(token, index=1)


def _decode_jwt_part(token: str, index: int) -> Mapping[str, Any]:
    try:
        part = token.split(".")[index]
        padded = part + "=" * (-len(part) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise StudyAuthError("Malformed JWT.") from exc

    if not isinstance(decoded, Mapping):
        raise StudyAuthError("Malformed JWT.")
    return decoded


def _validate_standard_claims(claims: Mapping[str, Any], config: McpConfig) -> None:
    now = int(time.time())
    expires_at = claims.get("exp")
    if isinstance(expires_at, (int, float)) and expires_at <= now:
        raise StudyAuthError("JWT has expired.")

    not_before = claims.get("nbf")
    if isinstance(not_before, (int, float)) and not_before > now:
        raise StudyAuthError("JWT is not valid yet.")

    issuer = config.cbioportal_auth_issuer
    if issuer and claims.get("iss") != issuer:
        raise StudyAuthError("JWT issuer did not match expected issuer.")

    audience = config.cbioportal_auth_audience
    if audience and not _claim_contains_audience(claims.get("aud"), audience):
        raise StudyAuthError("JWT audience did not match expected audience.")


def _claim_contains_audience(claim_audience: Any, expected_audience: str) -> bool:
    if isinstance(claim_audience, str):
        return claim_audience == expected_audience
    if isinstance(claim_audience, list):
        return expected_audience in claim_audience
    return False


def _has_http_context() -> bool:
    try:
        from fastmcp.server.dependencies import get_http_request

        get_http_request()
        return True
    except RuntimeError:
        return False


def _get_http_headers() -> Mapping[str, str]:
    from fastmcp.server.dependencies import get_http_headers

    return get_http_headers(include_all=True)
