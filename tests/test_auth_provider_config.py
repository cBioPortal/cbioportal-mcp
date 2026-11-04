"""Per-deployment login provider: defaults, fail-closed startup, Redis namespaces."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.memory import MemoryStore

from cbioportal_mcp import auth
from cbioportal_mcp.authentication.caller import resolve_caller_identity
from cbioportal_mcp.authentication.study_access import (
    StudyAccessConfigError,
    validate_study_access_startup_config,
)
from cbioportal_mcp.env import get_mcp_config

SECRET = "x" * 40
OIDC = {
    "CBIOPORTAL_MCP_AUTH_PROVIDER": "oidc",
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "jwt",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
    "CBIOPORTAL_MCP_OIDC_ISSUER_URL": "https://idp.example.org/realms/r",
    "CBIOPORTAL_MCP_OIDC_CLIENT_ID": "mcp",
    "CBIOPORTAL_MCP_OIDC_CLIENT_SECRET": SECRET,
    "CBIOPORTAL_MCP_OIDC_BASE_URL": "https://mcp.example.org",
    "CBIOPORTAL_MCP_OIDC_AUDIENCE": "cbioportal-mcp",
    "CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM": "cbio_studies",
}
DAT = {
    "CBIOPORTAL_MCP_AUTH_PROVIDER": "portal_dat",
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "portal_dat",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
    "CBIOPORTAL_MCP_PORTAL_URL": "https://portal.example.org",
    "CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET": SECRET,
    "CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF": "true",
}


@pytest.fixture
def env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CBIOPORTAL_MCP_") or name == "REDIS_URL":
            monkeypatch.delenv(name)

    def apply(values: dict, **overrides):
        merged = {**values, **overrides}
        for name, value in merged.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return get_mcp_config()

    return apply


# --- 1. every new flag at default -------------------------------------------


def test_defaults_are_the_base_behavior(env, monkeypatch):
    config = env({})
    assert config.auth_provider == "google"
    validate_study_access_startup_config(config)  # public mode: nothing required
    assert auth._build_auth_provider() is None  # no Google settings: unauthenticated

    env(
        {
            "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "id.apps.googleusercontent.com",
            "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": SECRET,
            "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.example.org",
        }
    )
    provider = auth._build_auth_provider()
    assert type(provider) is GoogleProvider
    assert provider.required_scopes == ["openid", "email", "profile"]


def test_default_identity_is_the_base_telemetry_resolution(env, monkeypatch):
    env({})
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include_all=False: {"x-user-id": "lc-1", "x-user-email": "a@example.org"},
    )
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: None)
    identity = resolve_caller_identity()
    assert identity.as_dict() == {
        "user_id": "lc-1",
        "email": "a@example.org",
        "groups": frozenset(),
        "studies": None,
        "provider": "google",
        "source": "public",
        "client_kind": "librechat",
    }


# --- 11. fail closed at startup -----------------------------------------------

INVALID = [
    ("unknown provider", OIDC, {"CBIOPORTAL_MCP_AUTH_PROVIDER": "keycloak"}, "Invalid"),
    ("oidc without issuer", OIDC, {"CBIOPORTAL_MCP_OIDC_ISSUER_URL": None}, "ISSUER_URL"),
    ("oidc without client id", OIDC, {"CBIOPORTAL_MCP_OIDC_CLIENT_ID": None}, "CLIENT_ID"),
    ("oidc without secret", OIDC, {"CBIOPORTAL_MCP_OIDC_CLIENT_SECRET": None}, "CLIENT_SECRET"),
    ("oidc without base url", OIDC, {"CBIOPORTAL_MCP_OIDC_BASE_URL": None}, "BASE_URL"),
    ("oidc without audience", OIDC, {"CBIOPORTAL_MCP_OIDC_AUDIENCE": None}, "AUDIENCE"),
    (
        "oidc http issuer",
        OIDC,
        {"CBIOPORTAL_MCP_OIDC_ISSUER_URL": "http://idp.example.org"},
        "https",
    ),
    ("oidc symmetric alg", OIDC, {"CBIOPORTAL_MCP_OIDC_SIGNING_ALGORITHM": "HS256"}, "asymmetric"),
    (
        "oidc on a public deployment",
        OIDC,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "public"},
        "private",
    ),
    (
        "jwt without studies claim",
        OIDC,
        {"CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM": None},
        "STUDIES_CLAIM",
    ),
    (
        "jwt without row policies",
        OIDC,
        {"CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "false"},
        "ROW_POLICY",
    ),
    (
        "jwt merged with acl file",
        OIDC,
        {"CBIOPORTAL_MCP_STUDY_ACL_FILE": "/x.json"},
        "never merged",
    ),
    (
        "jwt merged with static studies",
        OIDC,
        {"CBIOPORTAL_MCP_ALLOWED_STUDIES": "s"},
        "never merged",
    ),
    (
        "jwt with google provider",
        OIDC,
        {"CBIOPORTAL_MCP_AUTH_PROVIDER": "google"},
        "AUTH_PROVIDER=oidc",
    ),
    (
        "jwt with portal_dat provider",
        DAT,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "jwt"},
        "supplies no identity",
    ),
    ("portal_dat without portal url", DAT, {"CBIOPORTAL_MCP_PORTAL_URL": None}, "PORTAL_URL"),
    (
        "portal_dat http portal",
        DAT,
        {"CBIOPORTAL_MCP_PORTAL_URL": "http://portal.example.org"},
        "https",
    ),
    (
        "portal_dat without hmac secret",
        DAT,
        {"CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET": None},
        "HMAC_SECRET",
    ),
    (
        "portal_dat short hmac secret",
        DAT,
        {"CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET": "short"},
        "32",
    ),
    (
        "portal_dat bad cache seconds",
        DAT,
        {"CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS": "-1"},
        "CACHE_SECONDS",
    ),
    (
        "portal_dat unconfirmed show_unauthorized_studies",
        DAT,
        {"CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF": None},
        "show_unauthorized_studies",
    ),
    (
        "portal_dat without row policies",
        DAT,
        {"CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "false"},
        "ROW_POLICY",
    ),
    (
        "portal_dat on a public deployment",
        DAT,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "public"},
        "private",
    ),
    (
        "portal_dat with acl source",
        DAT,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "acl"},
        "supplies no identity",
    ),
    (
        "portal_dat with authorities source",
        DAT,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "authorities"},
        "supplies no identity",
    ),
    (
        "portal_dat merged with acl file",
        DAT,
        {"CBIOPORTAL_MCP_STUDY_ACL_FILE": "/x.json"},
        "never merged",
    ),
    (
        "portal_dat source with oidc provider",
        OIDC,
        {"CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "portal_dat"},
        "AUTH_PROVIDER=portal_dat",
    ),
    (
        "portal_dat source with google provider",
        DAT,
        {"CBIOPORTAL_MCP_AUTH_PROVIDER": "google"},
        "AUTH_PROVIDER=portal_dat",
    ),
    ("unknown source", OIDC, {"CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "ldap"}, "Invalid"),
]


@pytest.mark.parametrize(
    ("base", "overrides", "message"),
    [case[1:] for case in INVALID],
    ids=[case[0] for case in INVALID],
)
def test_invalid_combinations_refuse_to_start(env, base, overrides, message):
    with pytest.raises((StudyAccessConfigError, ValueError), match=message):
        validate_study_access_startup_config(env(base, **overrides))


@pytest.mark.parametrize(
    "values",
    [
        OIDC,
        DAT,
        {**OIDC, "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "authorities"},
        {
            **OIDC,
            "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "acl",
            "CBIOPORTAL_MCP_STUDY_ACL_FILE": "/x.json",
        },
        {
            **DAT,
            "CBIOPORTAL_MCP_PORTAL_URL": "http://portal:8080",
            "CBIOPORTAL_MCP_PORTAL_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING": "true",
        },
    ],
    ids=["oidc+jwt", "portal_dat", "oidc+authorities", "oidc+acl", "portal_dat local http"],
)
def test_valid_combinations_start(env, values):
    validate_study_access_startup_config(env(values))


# --- 10. Redis token store namespaces -------------------------------------------


def test_redis_namespaces_keep_google_keys_and_separate_providers(monkeypatch):
    shared = MemoryStore()
    monkeypatch.setattr(auth, "RedisStore", lambda url, default_collection: shared)
    google = auth._build_client_storage(SECRET, "redis://unused")
    oidc = auth._oidc_client_storage(SECRET, "redis://unused")

    async def scenario():
        # FastMCP's own collection names, as both providers use them.
        await google.put("client-1", {"who": "google"}, collection="mcp-oauth-proxy-clients")
        await oidc.put("client-1", {"who": "oidc"}, collection="mcp-oauth-proxy-clients")
        # Google keys are exactly where they were (no prefix).
        assert await shared.get("client-1", collection="mcp-oauth-proxy-clients") is not None
        assert (
            await shared.get(
                "client-1", collection=f"{auth.OIDC_STORAGE_PREFIX}__mcp-oauth-proxy-clients"
            )
            is not None
        )
        # Switching provider and back: both sessions still readable, never mixed.
        assert (await google.get("client-1", collection="mcp-oauth-proxy-clients")) == {
            "who": "google"
        }
        assert (await oidc.get("client-1", collection="mcp-oauth-proxy-clients")) == {"who": "oidc"}

    asyncio.run(scenario())
