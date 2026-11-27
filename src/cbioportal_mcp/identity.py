"""Google OAuth + trusted LibreChat bridge admission for FastMCP 2.13.

FastMCP 2's built-in HTTP auth wrapper requires a bearer on every request.
Mount Google's discovery/login routes separately so a bridge request (or a
rollout-exempt anonymous request) never needs a fabricated OAuth token.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from cbioportal_mcp import rollout
from cbioportal_mcp.env import get_mcp_config

logger = logging.getLogger(__name__)
BRIDGE_HEADERS = frozenset(
    {"x-user-email", "x-user-id", "x-cbioportal-mcp-bridge-secret", "x-user-email-verified"}
)
_STATE_KEY = "cbioportal_identity"
_CURRENT: ContextVar[Decision | None] = ContextVar("cbioportal_identity", default=None)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


def placeholder(value: str) -> bool:
    return "${" in value or "{{" in value


def validate_auth_startup() -> None:
    """Shared by every launcher, including local stdio. Never echo secrets."""
    config = get_mcp_config()
    google = (config.google_client_id, config.google_client_secret, config.google_base_url)
    present = [bool(value and value.strip()) for value in google]
    if any(value is not None for value in google) and not all(present):
        raise ValueError(
            "Partial Google OAuth configuration: set all three CBIOPORTAL_MCP_GOOGLE_* variables"
        )
    secret = os.getenv("CBIOPORTAL_MCP_BRIDGE_SECRET", "")
    if secret and (not secret.strip() or placeholder(secret)):
        raise ValueError("CBIOPORTAL_MCP_BRIDGE_SECRET must be non-empty and expanded")
    if rollout.get().auth_enforcement == "enforce" and not (all(present) or secret):
        raise ValueError("Auth enforce requires Google OAuth or CBIOPORTAL_MCP_BRIDGE_SECRET")


def normalized_email(value) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("b64:"):
        try:
            value = base64.b64decode(value[4:], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
    value = value.strip().lower()
    return value if _EMAIL.fullmatch(value) else None


def explicit_true(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    route: str


@dataclass(frozen=True)
class Decision:
    identity: Identity | None
    allowed: bool
    checked: bool


def google_identity(token) -> Identity | None:
    """Read only claims returned by Google's validated upstream access token.

    GoogleTokenVerifier nests v2 userinfo and v1 tokeninfo in these fields.
    Missing/unknown evidence fails closed; explicit negative evidence wins.
    """
    if token is None:
        return None
    claims = token.claims or {}
    user = claims.get("google_user_data") or {}
    info = claims.get("google_token_info") or {}
    if not isinstance(user, dict) or not isinstance(info, dict):
        return None
    evidence = [
        source[key]
        for source in (user, info)
        for key in ("verified_email", "email_verified")
        if key in source
    ]
    email = normalized_email(claims.get("email"))
    user_id = claims.get("sub")
    if not (evidence and all(explicit_true(value) for value in evidence) and email and user_id):
        return None
    return Identity(str(user_id), email, "oauth")


class CombinedAuth:
    """Keep Google's endpoints/verifier; share a single admission decision."""

    def __init__(self, google=None):
        self.google = google
        self.bridge_secret = os.getenv("CBIOPORTAL_MCP_BRIDGE_SECRET", "")

    async def decide(self, headers) -> Decision:
        bridge_present = any(name in headers for name in BRIDGE_HEADERS)
        oauth_present = "authorization" in headers
        duplicate = any(
            len(headers.getlist(name)) > 1 for name in BRIDGE_HEADERS | {"authorization"}
        )
        identities = []
        emails: list[str | None] = []
        valid = not duplicate
        full_valid = True
        has_login = False
        credential_emails: set[str] = set()
        if bridge_present:
            email = normalized_email(headers.get("x-user-email"))
            emails.extend(normalized_email(value) for value in headers.getlist("x-user-email"))
            supplied = headers.get("x-cbioportal-mcp-bridge-secret", "")
            # Compare bytes, including non-ASCII input, without a timing-leaky ==.
            matches = hmac.compare_digest(
                supplied.encode("utf-8"), self.bridge_secret.encode("utf-8")
            )
            secret_valid = bool(
                self.bridge_secret
                and self.bridge_secret.strip()
                and not placeholder(self.bridge_secret)
                and supplied
                and not placeholder(supplied)
                and matches
            )
            if "x-cbioportal-mcp-bridge-secret" in headers:
                valid = valid and secret_valid
                has_login = secret_valid
                if secret_valid and email:
                    credential_emails.add(email)
            user_id = headers.get("x-user-id", "").strip()
            bridge_valid = bool(
                secret_valid
                and explicit_true(headers.get("x-user-email-verified"))
                and email
                and user_id
            )
            full_valid = full_valid and bridge_valid
            if bridge_valid and email is not None:
                identities.append(Identity(user_id, email, "librechat"))
        if oauth_present:
            scheme, _, bearer = headers.get("authorization", "").partition(" ")
            token = None
            if self.google and scheme.lower() == "bearer" and bearer:
                try:
                    token = await self.google.verify_token(bearer)
                except Exception:
                    # Provider/network exceptions never expose tokens or claims in our logs.
                    logger.warning("auth provider verification failed")
            valid = valid and token is not None
            has_login = has_login or token is not None
            if token:
                email = normalized_email((token.claims or {}).get("email"))
                emails.append(email)
                if email:
                    credential_emails.add(email)
            identity = google_identity(token)
            full_valid = full_valid and identity is not None
            if identity:
                identities.append(identity)
        checked = rollout.auth_checks_apply(None) or any(
            rollout.auth_checks_apply(email) for email in emails
        )
        # Credentials always fail closed, independently of tester targeting.
        # Off preserves the existing Google login requirement, while a trusted
        # bridge secret also admits legacy non-testers without verified claims.
        valid = valid and len(credential_emails) <= 1
        allowed = (
            valid
            and (not checked or (full_valid and bool(identities)))
            and (has_login or (self.google is None and not checked))
        )
        identity = identities[-1] if allowed and full_valid and identities else None
        logger.info(
            "auth decision allowed=%s checked=%s route=%s",
            allowed,
            checked,
            identity.route if identity else "none",
        )
        return Decision(identity, allowed, checked)

    def mount(self, app, paths) -> None:
        if self.google:
            app.router.routes.extend(self.google.get_routes(mcp_path=paths[0]))
        app.add_middleware(IdentityHTTPMiddleware, auth=self, paths=paths)


class IdentityHTTPMiddleware:
    def __init__(self, app, auth: CombinedAuth, paths):
        self.app, self.auth, self.paths = app, auth, paths

    async def __call__(self, scope, receive, send):
        route_path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (route_path == root_path or route_path.startswith(root_path + "/")):
            route_path = route_path[len(root_path) :]
        if scope["type"] != "http" or not any(
            route_path.rstrip("/") == path.rstrip("/")
            or route_path.startswith(path.rstrip("/") + "/")
            for path in self.paths
        ):
            return await self.app(scope, receive, send)
        decision = await self.auth.decide(Request(scope).headers)
        if not decision.allowed:
            headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
            if self.auth.google:
                # FastMCP 2.13.3 private API: the real-provider integration
                # test pins this version and checks discovery/challenge URLs.
                resource_url = self.auth.google._get_resource_url(self.paths[0])
                from mcp.server.auth.routes import build_resource_metadata_url

                headers[
                    "WWW-Authenticate"
                ] += f', resource_metadata="{build_resource_metadata_url(resource_url)}"'
            return await JSONResponse(
                {"error": "invalid_token", "error_description": "Verified login required"},
                status_code=401,
                headers=headers,
            )(scope, receive, send)
        scope.setdefault("state", {})[_STATE_KEY] = decision
        # Untrusted identity headers must not reach telemetry or downstream
        # handlers, even when the caller is exempt during rollout.
        scope["headers"] = [
            (key, value)
            for key, value in scope["headers"]
            if key.decode("latin-1").lower() not in BRIDGE_HEADERS
        ]
        await self.app(scope, receive, send)


def request_decision() -> Decision | None:
    try:
        return get_http_request().scope.get("state", {}).get(_STATE_KEY) or _CURRENT.get()
    except RuntimeError:
        return _CURRENT.get()


class IdentityMiddleware(Middleware):
    """First MCP middleware: admission precedes every handler and cache."""

    def __init__(self, server):
        self.server = server

    async def on_message(self, context, call_next):
        decision = request_decision()
        if decision is None:
            # No HTTP admission means no user identity, including stdio. Only
            # explicitly opted-in local stdio is exempt from enforced login.
            local = (
                self.server.startup_transport == "stdio"
                and os.getenv("CBIOPORTAL_MCP_LOCAL_DEV") == "1"
            )
            if rollout.auth_checks_apply(None) and not local:
                raise ToolError("Verified login required")
        elif not decision.allowed:
            raise ToolError("Verified login required")
        token = _CURRENT.set(decision or Decision(None, True, False))
        try:
            return await call_next(context)
        finally:
            _CURRENT.reset(token)
