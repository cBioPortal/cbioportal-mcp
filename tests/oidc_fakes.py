"""Shared OIDC test fixtures: a local fake IdP served through httpx mocks."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from cbioportal_mcp.local_e2e_fakes import MockOidcIssuer

ISSUER = "https://idp.example.org/realms/test"
AUDIENCE = "cbioportal-mcp"

OIDC_ENV = {
    "CBIOPORTAL_MCP_AUTH_PROVIDER": "oidc",
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "jwt",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
    "CBIOPORTAL_MCP_OIDC_ISSUER_URL": ISSUER,
    "CBIOPORTAL_MCP_OIDC_CLIENT_ID": "mcp",
    "CBIOPORTAL_MCP_OIDC_CLIENT_SECRET": "oidc-client-secret-for-tests-0000",
    "CBIOPORTAL_MCP_OIDC_BASE_URL": "https://mcp.example.org",
    "CBIOPORTAL_MCP_OIDC_AUDIENCE": AUDIENCE,
    "CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM": "realm_access.roles",
    "CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX": "cbioportal:",
    "CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM": "groups",
}

# Unset so a developer's shell can't leak into these tests.
OIDC_CLEAR = (
    "CBIOPORTAL_MCP_OIDC_CONFIG_URL",
    "CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM",
    "CBIOPORTAL_MCP_OIDC_EMAIL_CLAIM",
    "CBIOPORTAL_MCP_OIDC_EMAIL_VERIFIED_CLAIM",
    "CBIOPORTAL_MCP_OIDC_TRUST_EMAIL_WITHOUT_VERIFIED_CLAIM",
    "CBIOPORTAL_MCP_OIDC_SIGNING_ALGORITHM",
    "CBIOPORTAL_MCP_OIDC_SCOPES",
    "CBIOPORTAL_MCP_STUDY_ACL_FILE",
    "CBIOPORTAL_MCP_ALLOWED_STUDIES",
    "CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS",
    "CBIOPORTAL_MCP_AUTH_PROXY_SECRET",
    "REDIS_URL",
)


def install_fake_idp(monkeypatch, issuer: MockOidcIssuer) -> None:
    """Route the verifier's JWKS fetch and OIDC discovery to ``issuer``."""
    from fastmcp.server.auth import oidc_proxy
    from fastmcp.server.auth.providers import jwt as jwt_module

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=issuer.discovery())
        if path.endswith("/v1/keys"):
            return httpx.Response(200, json=issuer.jwks())
        return httpx.Response(404)

    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        return real_async(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        jwt_module,
        "httpx",
        SimpleNamespace(AsyncClient=async_client, HTTPError=httpx.HTTPError),
    )

    def sync_get(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(url)

    monkeypatch.setattr(oidc_proxy, "httpx", SimpleNamespace(get=sync_get))


def keycloak_claims(issuer: MockOidcIssuer, *, user="alice", roles=(), **overrides) -> dict:
    """A Keycloak-shaped access token (realm roles, email_verified)."""
    import time

    now = int(time.time())
    claims = {
        "iss": issuer.issuer,
        "aud": [AUDIENCE, "account"],
        "exp": now + 300,
        "iat": now,
        "sub": f"kc-{user}",
        "email": f"{user}@example.org",
        "email_verified": True,
        "scope": "openid email profile",
        "groups": [f"/research/{user}"],
        "realm_access": {"roles": ["offline_access", *roles]},
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}
