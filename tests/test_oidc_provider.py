"""provider oidc: strict token verification, claim paths, email verification, jwt studies.

Tokens are signed by a local fake IdP (MockOidcIssuer); its JWKS and
discovery are served through httpx mocks. Two claim layouts run through the
same code - Keycloak-shaped and Okta-shaped - to show nothing is
IdP-specific.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from fastmcp.server.auth import AccessToken
from oidc_fakes import AUDIENCE, ISSUER, OIDC_CLEAR, OIDC_ENV, install_fake_idp, keycloak_claims

from cbioportal_mcp import auth
from cbioportal_mcp.authentication.caller import resolve_caller_identity, studies_from_jwt_claims
from cbioportal_mcp.authentication.study_access import (
    AuthorizationError,
    clickhouse_study_access_settings,
    get_current_study_access,
    study_access_cache_partition,
    verified_oauth_identity,
)
from cbioportal_mcp.env import get_mcp_config
from cbioportal_mcp.local_e2e_fakes import MockOidcIssuer


@pytest.fixture
def idp(monkeypatch):
    for name, value in OIDC_ENV.items():
        monkeypatch.setenv(name, value)
    for name in OIDC_CLEAR:
        monkeypatch.delenv(name, raising=False)
    issuer = MockOidcIssuer(ISSUER, AUDIENCE)
    install_fake_idp(monkeypatch, issuer)
    return issuer


def verifier(issuer: MockOidcIssuer) -> auth.StrictJWTVerifier:
    return auth.StrictJWTVerifier(
        jwks_uri=ISSUER + "/v1/keys", issuer=ISSUER, audience=[AUDIENCE], algorithm="RS256"
    )


def verify(issuer, token):
    return asyncio.run(verifier(issuer).verify_token(token))


def test_valid_token_is_accepted(idp):
    token = idp.mint(keycloak_claims(idp))
    access = verify(idp, token)
    assert access is not None and access.claims["sub"] == "kc-alice"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": None},  # no expiry
        {"exp": int(time.time()) - 10},  # expired
        {"exp": "never"},
        {"iss": None},
        {"iss": "https://other.example.org/realms/test"},
        {"iss": ISSUER + "/"},  # not verbatim
        {"aud": None},
        {"aud": "someone-else"},
        {"aud": ["account"]},
        {"nbf": int(time.time()) + 3600},
    ],
)
def test_tokens_failing_any_check_are_rejected(idp, overrides):
    assert verify(idp, idp.mint(keycloak_claims(idp, **overrides))) is None


def test_bad_signature_wrong_key_and_alg_are_rejected(idp):
    good = idp.mint(keycloak_claims(idp))
    header, payload, sig = good.split(".")
    assert verify(idp, f"{header}.{payload}.{sig[:-4]}AAAA") is None
    other = MockOidcIssuer(ISSUER, AUDIENCE)
    assert verify(idp, other.mint(keycloak_claims(idp), kid=idp.kid)) is None
    unsigned = idp.mint(keycloak_claims(idp)).split(".")
    import base64
    import json

    none_header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    assert verify(idp, none_header.decode() + "." + unsigned[1] + ".") is None


def as_token(claims: dict):
    token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)
    return patch("fastmcp.server.dependencies.get_access_token", return_value=token)


def test_keycloak_layout_roles_become_studies(idp):
    claims = keycloak_claims(idp, roles=["cbioportal:study_alpha", "cbioportal:study_public"])
    with as_token(claims), patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        caller = resolve_caller_identity(include_studies=True)
        access = get_current_study_access()
        settings = clickhouse_study_access_settings()
    assert caller.user_id == "kc-alice" and caller.email == "alice@example.org"
    assert caller.groups == {"/research/alice"} and caller.provider == "oidc"
    assert caller.source == "jwt" and caller.client_kind == "oauth"
    assert caller.studies == access.studies == {"study_alpha", "study_public"}
    assert settings == {"SQL_cbiomcp_allowed_studies": "study_alpha,study_public"}


def test_okta_layout_through_configuration_only(monkeypatch, idp):
    monkeypatch.setenv("CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM", "uid")
    monkeypatch.setenv("CBIOPORTAL_MCP_OIDC_EMAIL_CLAIM", "sub")
    monkeypatch.setenv("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM", "cbio_studies")
    monkeypatch.delenv("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX")
    idp.audience = AUDIENCE
    idp.users["alice@example.org"] = {"uid": "00u1", "groups": ["g1"], "studies": ["study_alpha"]}
    token = idp.access_token_for("alice@example.org")
    access = verify(idp, token)
    assert access is not None
    with as_token(access.claims):
        caller = resolve_caller_identity(include_studies=True)
    assert caller.user_id == "00u1" and caller.studies == {"study_alpha"}


def test_namespaced_claim_names_with_dots(monkeypatch, idp):
    monkeypatch.setenv("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM", "https://example.org/studies")
    monkeypatch.delenv("CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX")
    claims = keycloak_claims(idp, **{"https://example.org/studies": ["study_beta"]})
    assert studies_from_jwt_claims(claims, get_mcp_config()) == {"study_beta"}


@pytest.mark.parametrize(
    "roles_value",
    [
        "MISSING",  # claim absent: deny, no ACL fallback
        {"not": "a list"},
        [1, 2],
        ["cbioportal:*"],  # no wildcard
        ["cbioportal:bad id"],
    ],
)
def test_bad_or_missing_studies_claim_denies(idp, roles_value):
    claims = keycloak_claims(idp)
    if roles_value == "MISSING":
        del claims["realm_access"]
    else:
        claims["realm_access"] = {"roles": roles_value}
    with as_token(claims), pytest.raises(AuthorizationError):
        get_current_study_access()


def test_empty_studies_claim_means_no_studies(idp):
    with as_token(keycloak_claims(idp, roles=["offline_access"])):
        assert get_current_study_access().studies == frozenset()


def test_acl_file_is_never_merged(monkeypatch, idp, tmp_path):
    acl = tmp_path / "acl.json"
    acl.write_text('{"default": ["study_beta"], "users": {"kc-alice": ["study_beta"]}}')
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACL_FILE", str(acl))
    claims = keycloak_claims(idp, roles=["cbioportal:study_alpha"])
    with as_token(claims):
        assert get_current_study_access().studies == {"study_alpha"}


# --- email verification (contract item 3) ------------------------------------


@pytest.mark.parametrize(
    "verified, trust, ok",
    [
        (True, False, True),
        ("MISSING", False, False),
        ("MISSING", True, True),  # explicit trust decision
        (False, True, False),  # present-but-false is refused even with the opt-out
        ("true", True, False),  # malformed
        (1, True, False),
        (None, True, False),  # present as null
    ],
)
def test_oidc_email_verification(monkeypatch, idp, verified, trust, ok):
    monkeypatch.setenv("CBIOPORTAL_MCP_OIDC_TRUST_EMAIL_WITHOUT_VERIFIED_CLAIM", str(trust).lower())
    claims = keycloak_claims(idp)
    if verified == "MISSING":
        del claims["email_verified"]
    else:
        claims["email_verified"] = verified
    with as_token(claims):
        if ok:
            assert verified_oauth_identity() == ("kc-alice", "alice@example.org")
        else:
            with pytest.raises(AuthorizationError):
                verified_oauth_identity()


def test_email_verification_errors_fail_closed(idp):
    with patch("fastmcp.server.dependencies.get_access_token", side_effect=RuntimeError("x")):
        with pytest.raises(AuthorizationError):
            verified_oauth_identity()
    with as_token(keycloak_claims(idp, email=["a@b"])), pytest.raises(AuthorizationError):
        verified_oauth_identity()


def test_google_shape_unchanged_and_refused_for_oidc(monkeypatch, idp):
    google_claims = {
        "sub": "g-1",
        "email": "alice@example.org",
        "google_user_data": {"email": "alice@example.org", "verified_email": True},
    }
    with as_token(google_claims), pytest.raises(AuthorizationError):
        verified_oauth_identity()  # oidc needs email_verified, not Google's records
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROVIDER", "google")
    with as_token(google_claims):
        assert verified_oauth_identity() == ("g-1", "alice@example.org")
    with as_token(keycloak_claims(idp)), pytest.raises(AuthorizationError):
        verified_oauth_identity()  # Google path still demands its own records


def test_authorities_source_with_oidc_sends_verified_email(monkeypatch, idp):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "authorities")
    with as_token(keycloak_claims(idp, email="Alice@Example.org")):
        assert clickhouse_study_access_settings() == {"SQL_cbio_user": "alice@example.org"}
    with as_token(keycloak_claims(idp, email_verified=False)), pytest.raises(AuthorizationError):
        clickhouse_study_access_settings()


def test_acl_source_with_oidc_uses_token_not_headers(monkeypatch, idp, tmp_path):
    acl = tmp_path / "acl.json"
    acl.write_text('{"groups": {"/research/alice": ["study_alpha"]}, "users": {"mallory": ["x"]}}')
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "acl")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACL_FILE", str(acl))
    spoof = {"x-user-id": "mallory", "x-cbioportal-allowed-studies": "study_beta"}
    with (
        as_token(keycloak_claims(idp)),
        patch("fastmcp.server.dependencies.get_http_headers", return_value=spoof),
    ):
        assert get_current_study_access().studies == {"study_alpha"}


def test_cache_partition_is_per_caller(idp):
    roles = ["cbioportal:study_alpha"]
    with as_token(keycloak_claims(idp, user="alice", roles=roles)):
        alice = study_access_cache_partition()
    with as_token(keycloak_claims(idp, user="carol", roles=roles)):
        carol = study_access_cache_partition()
    assert alice != carol and "study_alpha" in alice


def test_no_token_means_no_identity_and_no_header_fallback(idp):
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "spoof"}),
    ):
        caller = resolve_caller_identity()
        assert caller.user_id is None and caller.client_kind == "direct"
        with pytest.raises(AuthorizationError):
            get_current_study_access()


# --- provider construction ------------------------------------------------------


def test_build_oidc_provider_from_discovery(monkeypatch, idp):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
    provider = auth._build_auth_provider()
    assert isinstance(provider, auth.OIDCProxy)
    validator = provider._token_validator
    assert isinstance(validator, auth.StrictJWTVerifier)
    assert validator.issuer == ISSUER and validator.audience == [AUDIENCE]
    assert validator.jwks_uri == ISSUER + "/v1/keys"
    # Separate Redis namespace for oidc; FastMCP's collections are prefixed.
    storage = provider._client_storage
    assert isinstance(storage, auth.PrefixCollectionsWrapper)
    assert storage.prefix == auth.OIDC_STORAGE_PREFIX


def test_build_refuses_mismatched_discovery_issuer(monkeypatch, idp):
    idp.issuer = "https://impostor.example.org"
    with pytest.raises(ValueError, match="issuer"):
        auth._build_auth_provider()
