"""Local test doubles for the per-deployment login e2e and unit tests.

Never used by the server. Two fakes, both usable in-process (an httpx
handler) and as a container (a Starlette app run by uvicorn):

FakePortal
    The subset of cBioPortal the MCP's portal_dat mode reads, with the
    portal's real semantics at 5677496: ``GET /api/studies`` lists only the
    token's READ-able studies and answers 401 without credentials;
    ``GET /api/data-access-tokens`` returns the owner's tokens *with their
    secrets*, like the real uuid implementation. It records every request so
    tests can assert the MCP never POSTs/DELETEs and never calls
    ``/api/data-access-tokens/{token}``. ``/_test/*`` control routes
    (revoke, outage, ...) exist only on the container app.

MockOidcIssuer
    An OIDC issuer with an Okta-shaped access-token layout (``uid`` user id,
    login in ``sub``, ``groups`` and a custom ``cbio_studies`` claim, no
    ``email_verified``), auto-approving the user named by ``login_hint``. It
    proves the MCP's OIDC mode needs nothing Keycloak-specific.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# --- fake cBioPortal -----------------------------------------------------------


@dataclass
class PortalUser:
    username: str
    studies: list[str]
    tokens: list[str] = field(default_factory=list)


class FakePortal:
    def __init__(self) -> None:
        self.users: dict[str, PortalUser] = {}
        self.token_owner: dict[str, str] = {}
        self.token_expiration: dict[str, str] = {}
        self.requests: list[tuple[str, str, bool]] = []  # (method, path, had_auth)
        self.outage = False
        self.anonymous_allowed = False  # simulate authenticate=false
        self.token_list_status = 200  # e.g. 403 (download_group) or 500 (oauth2)
        self.studies_body_override = None
        self._lock = threading.Lock()

    def add_user(self, username: str, studies: list[str], token: str | None = None) -> str:
        token = token or secrets.token_hex(16)
        with self._lock:
            user = self.users.setdefault(username, PortalUser(username, list(studies)))
            user.studies = list(studies)
            user.tokens.append(token)
            self.token_owner[token] = username
            self.token_expiration[token] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 30 * 86400)
            )
        return token

    def revoke_user(self, username: str) -> None:
        with self._lock:
            for token in self.users[username].tokens:
                self.token_owner.pop(token, None)
            self.users[username].tokens.clear()

    def handle(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, object]:
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        with self._lock:
            self.requests.append((method, path, token is not None))
            if self.outage:
                return 503, {"message": "Service Unavailable"}
            if method != "GET":
                return 405, {"message": "fake portal is read-only"}
            if path.startswith("/api/data-access-tokens/"):
                return 418, {"message": "the MCP must never call this"}
            owner = self.token_owner.get(token) if token else None
            if path == "/api/studies":
                if owner is None:
                    if token is None and self.anonymous_allowed:
                        return 200, [{"studyId": s} for u in self.users.values() for s in u.studies]
                    return 401, {"message": "Unauthorized"}
                if self.studies_body_override is not None:
                    return 200, self.studies_body_override
                return 200, [
                    # readPermission deliberately misleading: never consulted.
                    {"studyId": s, "name": s, "publicStudy": False, "readPermission": False}
                    for s in self.users[owner].studies
                ]
            if path == "/api/data-access-tokens":
                if owner is None:
                    return 401, {"message": "Unauthorized"}
                if self.token_list_status != 200:
                    return self.token_list_status, {"message": "unsupported"}
                user = self.users[owner]
                return 200, [
                    {
                        "token": t,
                        "username": owner,
                        "expiration": self.token_expiration.get(t),
                        "creation": "2026-01-01 00:00:00",
                    }
                    for t in user.tokens
                ]
            return 404, {"message": "Not Found"}

    def httpx_handler(self, request: httpx.Request) -> httpx.Response:
        status, body = self.handle(
            request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()}
        )
        return httpx.Response(status, json=body)


def fake_portal_app(portal: FakePortal | None = None):
    """Starlette app for the e2e container, with /_test control routes."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    portal = portal or FakePortal()
    seed = json.loads(os.getenv("FAKE_PORTAL_USERS", "{}"))
    for username, spec in seed.items():
        portal.add_user(username, spec["studies"], spec.get("token"))

    async def api(request: Request):
        status, body = portal.handle(
            request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()}
        )
        return JSONResponse(body, status_code=status)

    async def control(request: Request):
        action = request.path_params["action"]
        params = dict(request.query_params)
        if action == "revoke":
            portal.revoke_user(params["user"])
        elif action == "outage":
            portal.outage = params.get("on") == "1"
        elif action == "requests":
            return JSONResponse(portal.requests)
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/_test/{action}", control, methods=["POST", "GET"]),
            Route("/{path:path}", api, methods=["GET", "POST", "DELETE", "PUT"]),
        ]
    )


# --- mock OIDC issuer (Okta-shaped claims) --------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class MockOidcIssuer:
    """Minimal OIDC authorization-code issuer with an Okta-like access token."""

    def __init__(self, issuer: str, audience: str = "api://default") -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "mock-" + secrets.token_hex(4)
        self.users: dict[str, dict] = {}
        # Who "logs in" when the authorize request names nobody (MCP proxies
        # do not forward login_hint); tests set it before each flow.
        self.default_login: str | None = os.getenv("MOCK_OIDC_DEFAULT_LOGIN")
        self._codes: dict[str, dict] = {}

    # keys / tokens
    def jwks(self) -> dict:
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": _b64url(numbers.e.to_bytes(3, "big")),
                }
            ]
        }

    def private_pem(self) -> str:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def mint(self, claims: dict, *, kid: str | None = None, alg: str = "RS256") -> str:
        from authlib.jose import JsonWebKey, JsonWebToken

        if not hasattr(self, "_signing_jwk"):
            # Importing the PEM is slow (key checks); do it once per issuer.
            self._signing_jwk = JsonWebKey.import_key(self.private_pem(), {"kty": "RSA"})
        header = {"alg": alg, "kid": kid or self.kid, "typ": "JWT"}
        return JsonWebToken([alg]).encode(header, claims, self._signing_jwk).decode()

    def access_token_for(self, login: str, *, lifetime: int = 3600) -> str:
        user = self.users[login]
        now = int(time.time())
        claims = {
            "ver": 1,
            "jti": "AT." + secrets.token_hex(8),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": now + lifetime,
            "cid": "mock-client",
            "uid": user["uid"],
            "scp": ["openid", "email", "profile"],
            "sub": login,
            "groups": user.get("groups", []),
        }
        if "studies" in user:  # omitted entirely for users with no grant
            claims["cbio_studies"] = user["studies"]
        return self.mint(claims)

    def discovery(self, backchannel: str | None = None) -> dict:
        back = (backchannel or self.issuer).rstrip("/")
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/v1/authorize",
            "token_endpoint": f"{back}/v1/token",
            "jwks_uri": f"{back}/v1/keys",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "email", "profile"],
        }

    def authorize_redirect(self, query: dict[str, str]) -> str:
        login = query.get("login_hint") or self.default_login or ""
        if login not in self.users:
            return (
                query["redirect_uri"]
                + "?"
                + urlencode({"error": "access_denied", "state": query.get("state", "")})
            )
        code = secrets.token_urlsafe(16)
        self._codes[code] = {
            "login": login,
            "redirect_uri": query.get("redirect_uri"),
            "challenge": query.get("code_challenge"),
        }
        return (
            query["redirect_uri"] + "?" + urlencode({"code": code, "state": query.get("state", "")})
        )

    def exchange(self, form: dict[str, str]) -> tuple[int, dict]:
        entry = self._codes.pop(form.get("code", ""), None)
        if entry is None or entry["redirect_uri"] != form.get("redirect_uri"):
            return 400, {"error": "invalid_grant"}
        if entry["challenge"]:
            verifier = form.get("code_verifier", "")
            digest = _b64url(hashlib.sha256(verifier.encode()).digest())
            if digest != entry["challenge"]:
                return 400, {"error": "invalid_grant"}
        return 200, {
            "access_token": self.access_token_for(entry["login"]),
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email profile",
        }


def mock_oidc_app(issuer: MockOidcIssuer | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.routing import Route

    issuer = issuer or MockOidcIssuer(
        os.environ["MOCK_OIDC_ISSUER"], os.getenv("MOCK_OIDC_AUDIENCE", "api://default")
    )
    issuer.users.update(json.loads(os.getenv("MOCK_OIDC_USERS", "{}")))
    prefix = urlparse(issuer.issuer).path.rstrip("/")

    def backchannel(request: Request) -> str:
        return f"{request.url.scheme}://{request.headers.get('host')}{prefix}"

    async def discovery(request: Request):
        return JSONResponse(issuer.discovery(backchannel(request)))

    async def keys(_: Request):
        return JSONResponse(issuer.jwks())

    async def authorize(request: Request):
        return RedirectResponse(issuer.authorize_redirect(dict(request.query_params)), 302)

    async def token(request: Request):
        form = parse_qs((await request.body()).decode())
        status, body = issuer.exchange({k: v[0] for k, v in form.items()})
        return JSONResponse(body, status_code=status)

    return Starlette(
        routes=[
            Route(f"{prefix}/.well-known/openid-configuration", discovery),
            Route(f"{prefix}/v1/keys", keys),
            Route(f"{prefix}/v1/authorize", authorize),
            Route(f"{prefix}/v1/token", token, methods=["POST"]),
        ]
    )


def main() -> None:
    """Run one fake: ``LOCAL_E2E_FAKE=portal|oidc``."""
    import uvicorn

    kind = os.getenv("LOCAL_E2E_FAKE", "portal")
    app = fake_portal_app() if kind == "portal" else mock_oidc_app()
    uvicorn.run(
        app, host=os.getenv("FAKE_BIND_HOST", "0.0.0.0"), port=int(os.getenv("FAKE_PORT", "8080"))
    )


if __name__ == "__main__":
    main()
