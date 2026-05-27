import asyncio
import base64
import json
import time

import pytest
from fastmcp.server.auth.providers.bearer import RSAKeyPair

from cbioportal_mcp.authentication.request_context import (
    StudyAuthError,
    StudyAuthMiddleware,
    get_current_study_scope,
    set_current_study_scope,
    study_scope_from_authorization_header,
)
from cbioportal_mcp.authentication.study_scope import StudyScope
from cbioportal_mcp.env import McpConfig


def _unsigned_token(claims: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return ".".join([_b64_json(header), _b64_json(claims), ""])


def _b64_json(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    for env_var in [
        "CBIOPORTAL_AUTH_ENABLED",
        "CBIOPORTAL_AUTH_REQUIRED",
        "CBIOPORTAL_KEYCLOAK_CLIENT_ID",
        "CBIOPORTAL_AUTH_ISSUER",
        "CBIOPORTAL_AUTH_AUDIENCE",
        "CBIOPORTAL_AUTH_JWKS_URI",
        "CBIOPORTAL_AUTH_PUBLIC_KEY",
        "CBIOPORTAL_AUTH_JWT_ALGORITHM",
        "CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV",
        "CBIOPORTAL_ALL_STUDIES_ROLE",
        "CBIOPORTAL_DEFAULT_STUDIES",
    ]:
        monkeypatch.delenv(env_var, raising=False)


def test_valid_bearer_token_with_study_roles(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV", "true")
    token = _unsigned_token(
        {
            "resource_access": {
                "cbioportal": {
                    "roles": ["brca_tcga", "luad_tcga"],
                }
            }
        }
    )

    scope = study_scope_from_authorization_header(f"Bearer {token}", McpConfig())

    assert scope.allowed_studies == frozenset({"brca_tcga", "luad_tcga"})
    assert scope.allow_all is False


def test_valid_signed_bearer_token_with_study_roles(monkeypatch):
    key_pair = RSAKeyPair.generate()
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_PUBLIC_KEY", key_pair.public_key)
    monkeypatch.setenv("CBIOPORTAL_AUTH_ISSUER", "https://issuer.example")
    monkeypatch.setenv("CBIOPORTAL_AUTH_AUDIENCE", "cbioportal-api")
    token = key_pair.create_token(
        subject="alice",
        issuer="https://issuer.example",
        audience="cbioportal-api",
        additional_claims={
            "resource_access": {
                "cbioportal": {
                    "roles": ["brca_tcga"],
                }
            }
        },
    )

    scope = study_scope_from_authorization_header(f"Bearer {token}", McpConfig())

    assert scope.allowed_studies == frozenset({"brca_tcga"})
    assert scope.allow_all is False


def test_missing_bearer_token_when_auth_required(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "true")

    with pytest.raises(StudyAuthError, match="Missing Authorization"):
        study_scope_from_authorization_header(None, McpConfig())


def test_missing_bearer_token_when_auth_disabled():
    scope = study_scope_from_authorization_header(None, McpConfig())

    assert scope.allow_all is True
    assert scope.source == "auth_disabled"


def test_malformed_bearer_token(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "true")

    with pytest.raises(StudyAuthError, match="Malformed Authorization"):
        study_scope_from_authorization_header("Basic abc123", McpConfig())


def test_unverified_jwt_requires_explicit_dev_flag(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    token = _unsigned_token({"resource_access": {"cbioportal": {"roles": ["brca_tcga"]}}})

    with pytest.raises(StudyAuthError, match="JWT verification is not configured"):
        study_scope_from_authorization_header(f"Bearer {token}", McpConfig())


def test_unverified_jwt_still_validates_issuer_and_audience(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ISSUER", "https://issuer.example")
    monkeypatch.setenv("CBIOPORTAL_AUTH_AUDIENCE", "cbioportal-api")
    token = _unsigned_token(
        {
            "iss": "https://issuer.example",
            "aud": ["other-api", "cbioportal-api"],
            "resource_access": {"cbioportal": {"roles": ["brca_tcga"]}},
        }
    )

    scope = study_scope_from_authorization_header(f"Bearer {token}", McpConfig())

    assert scope.allowed_studies == frozenset({"brca_tcga"})


def test_request_local_separation_between_concurrent_scopes():
    async def read_scope(scope: StudyScope) -> StudyScope:
        with set_current_study_scope(scope):
            await asyncio.sleep(0)
            return get_current_study_scope(McpConfig())

    async def run_concurrently():
        return await asyncio.gather(read_scope(alice), read_scope(bob))

    alice = StudyScope(frozenset({"brca_tcga"}), source="test")
    bob = StudyScope(frozenset({"luad_tcga"}), source="test")

    alice_seen, bob_seen = asyncio.run(run_concurrently())

    assert alice_seen == alice
    assert bob_seen == bob


def test_stdio_fallback_behavior_when_auth_required(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "true")

    scope = get_current_study_scope(McpConfig())

    assert scope.allow_all is False
    assert scope.allowed_studies == frozenset()
    assert scope.source == "no_claims"


def test_expired_unverified_jwt_is_rejected(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_ALLOW_UNVERIFIED_JWT_FOR_DEV", "true")
    token = _unsigned_token(
        {
            "exp": int(time.time()) - 60,
            "resource_access": {"cbioportal": {"roles": ["brca_tcga"]}},
        }
    )

    with pytest.raises(StudyAuthError, match="expired"):
        study_scope_from_authorization_header(f"Bearer {token}", McpConfig())


def test_auth_required_middleware_fails_closed_without_bearer(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_AUTH_REQUIRED", "true")
    sent_messages = []

    async def app(scope, receive, send):
        raise AssertionError("downstream app should not be called")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent_messages.append(message)

    middleware = StudyAuthMiddleware(app, config=McpConfig())
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
    }

    asyncio.run(middleware(scope, receive, send))

    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 401
