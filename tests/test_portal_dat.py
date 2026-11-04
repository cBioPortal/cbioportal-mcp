"""portal_dat: study access from a pasted cBioPortal Data Access Token.

Against a fake portal (cbioportal_mcp.local_e2e_fakes.FakePortal) with the
real portal's semantics at 5677496. Covers: allowed studies are exactly the
listed studies (readPermission ignored), every failure denies, the short
per-token cache (revocation and outage take effect after its TTL, never past
a known token expiry), the owner lookup is telemetry only, the MCP never
POSTs/DELETEs or puts the token in a URL, and the raw token never reaches
logs, exceptions, telemetry or cache keys.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import httpx
import pytest

from cbioportal_mcp import server
from cbioportal_mcp.authentication import portal_dat
from cbioportal_mcp.authentication.caller import resolve_caller_identity
from cbioportal_mcp.authentication.study_access import (
    AuthorizationError,
    clickhouse_study_access_settings,
    get_current_study_access,
    study_access_cache_partition,
)
from cbioportal_mcp.env import get_mcp_config
from cbioportal_mcp.local_e2e_fakes import FakePortal

ALICE_TOKEN = "11111111-aaaa-4bbb-8ccc-alicealicealice"
BOB_TOKEN = "22222222-aaaa-4bbb-8ccc-bobbobbobbobbob"
SECRET = "test-hmac-secret-0123456789abcdef0123"

DAT_ENV = {
    "CBIOPORTAL_MCP_AUTH_PROVIDER": "portal_dat",
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "portal_dat",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
    "CBIOPORTAL_MCP_PORTAL_URL": "https://portal.example.org",
    "CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET": SECRET,
    "CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF": "true",
    "CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS": "30",
}


@pytest.fixture
def portal(monkeypatch):
    for name, value in DAT_ENV.items():
        monkeypatch.setenv(name, value)
    for name in (
        "CBIOPORTAL_MCP_STUDY_ACL_FILE",
        "CBIOPORTAL_MCP_ALLOWED_STUDIES",
        "CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS",
        "CBIOPORTAL_MCP_PORTAL_USERNAMES_ARE_EMAILS",
    ):
        monkeypatch.delenv(name, raising=False)
    fake = FakePortal()
    fake.add_user("alice@example.org", ["study_alpha", "study_public"], ALICE_TOKEN)
    fake.add_user("bob", ["study_beta"], BOB_TOKEN)
    monkeypatch.setattr(portal_dat, "_transport_for_tests", httpx.MockTransport(fake.httpx_handler))
    portal_dat.reset_for_tests()
    yield fake
    portal_dat.reset_for_tests()


def as_caller(token: str | None, **extra_headers):
    headers = dict(extra_headers)
    if token is not None:
        headers["x-auth-token"] = token
    return patch("fastmcp.server.dependencies.get_http_headers", return_value=headers)


def test_allowed_studies_are_exactly_the_portal_list(portal):
    with as_caller(ALICE_TOKEN):
        access = get_current_study_access()
    assert access.studies == {"study_alpha", "study_public"}
    assert not access.all_studies
    # readPermission=false on every row is ignored (never trusted either way).
    with as_caller(BOB_TOKEN):
        assert get_current_study_access().studies == {"study_beta"}


def test_study_ids_reach_clickhouse_via_the_allowlist_setting(portal):
    with as_caller(ALICE_TOKEN):
        settings = clickhouse_study_access_settings()
    assert settings == {"SQL_cbiomcp_allowed_studies": "study_alpha,study_public"}


def test_portal_url_comes_only_from_config(portal):
    with as_caller(ALICE_TOKEN, host="evil.example", **{"x-cbioportal-url": "https://evil"}):
        get_current_study_access()
    assert portal.requests  # every request went to the fake (configured URL)


@pytest.mark.parametrize("token", [None, "", "not a token", "x" * 5000, "abc\r\nx-y: z"])
def test_missing_or_malformed_token_denies(portal, token):
    with as_caller(token), pytest.raises(AuthorizationError):
        get_current_study_access()
    assert portal.requests == []


def test_unknown_token_denies(portal):
    with as_caller("33333333-dead-beef-0000-unknownunknown"), pytest.raises(AuthorizationError):
        get_current_study_access()


@pytest.mark.parametrize(
    "body",
    [
        {"studyId": "x"},
        [{"name": "no id"}],
        [{"studyId": "bad id; drop"}],
        [{"studyId": "*"}],
        [{"studyId": 7}],
        "nope",
    ],
)
def test_malformed_study_list_denies(portal, body):
    portal.studies_body_override = body
    with as_caller(ALICE_TOKEN), pytest.raises(AuthorizationError):
        get_current_study_access()


def test_portal_that_serves_api_anonymously_denies(portal):
    portal.anonymous_allowed = True  # authenticate=false / optional_oauth2
    with as_caller(ALICE_TOKEN), pytest.raises(AuthorizationError, match="without authentication"):
        get_current_study_access()


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")]
)
def test_transport_errors_deny(monkeypatch, portal, error):
    def raise_(request):
        raise error

    monkeypatch.setattr(portal_dat, "_transport_for_tests", httpx.MockTransport(raise_))
    with as_caller(ALICE_TOKEN), pytest.raises(AuthorizationError):
        get_current_study_access()


@pytest.mark.parametrize("status", [302, 500, 502, 503])
def test_portal_error_statuses_deny(monkeypatch, portal, status):
    def handler(request):
        if request.headers.get("authorization"):
            return httpx.Response(status, headers={"location": "https://elsewhere"})
        return httpx.Response(401)

    monkeypatch.setattr(portal_dat, "_transport_for_tests", httpx.MockTransport(handler))
    with as_caller(ALICE_TOKEN), pytest.raises(AuthorizationError):
        get_current_study_access()


def test_cache_hit_avoids_portal_and_revocation_applies_after_ttl(monkeypatch, portal):
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS", "0.3")
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
        calls = len(portal.requests)
        get_current_study_access()
        assert len(portal.requests) == calls  # cached
        portal.revoke_user("alice@example.org")
        assert get_current_study_access().studies  # within TTL
        time.sleep(0.35)
        with pytest.raises(AuthorizationError):
            get_current_study_access()


def test_outage_denies_after_ttl(monkeypatch, portal):
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS", "0.2")
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
        portal.outage = True
        time.sleep(0.25)
        with pytest.raises(AuthorizationError):
            get_current_study_access()


def test_cache_never_outlives_known_token_expiry(monkeypatch, portal):
    portal.token_expiration[ALICE_TOKEN] = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 5)
    )
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
    assert portal_dat._cache.keys() == []  # already expired: not cached at all


def test_cache_disabled_with_zero_ttl(monkeypatch, portal):
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS", "0")
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
    assert portal_dat._cache.keys() == []


def test_cache_keys_are_portal_and_hmac_not_the_token(portal):
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
    with as_caller(BOB_TOKEN):
        get_current_study_access()
    keys = portal_dat._cache.keys()
    assert len(keys) == 2  # per caller
    for base_url, digest in keys:
        assert base_url == "https://portal.example.org"
        assert len(digest) == 64 and ALICE_TOKEN not in digest and BOB_TOKEN not in digest
    assert ALICE_TOKEN not in repr(portal_dat._cache._entries)


def test_only_reads_and_never_token_in_url(portal):
    with as_caller(ALICE_TOKEN):
        get_current_study_access()
    assert {m for m, _p, _a in portal.requests} == {"GET"}
    paths = {p for _m, p, _a in portal.requests}
    assert paths == {"/api/studies", "/api/data-access-tokens"}
    assert not any(ALICE_TOKEN in p for p in paths)


def test_owner_identity_is_namespaced_username_telemetry_only(monkeypatch, portal):
    with as_caller(ALICE_TOKEN):
        caller = resolve_caller_identity()
    assert caller.user_id == "portal:portal.example.org:alice@example.org"
    assert caller.email is None  # usernames-are-emails not confirmed
    assert caller.client_kind == "portal_dat" and caller.provider == "portal_dat"
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_USERNAMES_ARE_EMAILS", "true")
    portal_dat.reset_for_tests()
    with as_caller(ALICE_TOKEN):
        caller = resolve_caller_identity()
        access = get_current_study_access()
    assert caller.email == "alice@example.org"
    # Never an access key: no verified email is exposed to study access.
    assert access.identity.user_email is None


@pytest.mark.parametrize("status", [403, 500, 501])
def test_owner_lookup_failure_falls_back_to_fingerprint(portal, status):
    portal.token_list_status = status  # download_group / oauth2 portals
    with as_caller(ALICE_TOKEN):
        caller = resolve_caller_identity()
        access = get_current_study_access()
    assert access.studies == {"study_alpha", "study_public"}  # access unaffected
    assert caller.user_id.startswith("portal:portal.example.org:fp:")
    assert ALICE_TOKEN not in caller.user_id


def test_oauth2_style_token_uses_sub_hint(portal):
    import base64
    import json

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    jwt_like = ".".join(
        [seg({"alg": "RS256"}), seg({"sub": "kc-user-9", "exp": time.time() + 600}), "sig"]
    )
    portal.add_user("ignored", ["study_beta"], jwt_like)
    portal.token_list_status = 501
    with as_caller(jwt_like):
        caller = resolve_caller_identity()
    assert caller.user_id == "portal:portal.example.org:sub:kc-user-9"


def test_cache_partition_and_guides_are_per_caller(monkeypatch, portal):
    """Neither cached query results nor cached study guides cross callers."""
    portal.add_user("carol", ["study_alpha", "study_public"], "carol-token-0000000000000000")
    with as_caller(ALICE_TOKEN):
        alice = study_access_cache_partition()
    with as_caller("carol-token-0000000000000000"):
        carol = study_access_cache_partition()
    with as_caller(BOB_TOKEN):
        bob = study_access_cache_partition()
    assert len({alice, bob, carol}) == 3  # same studies, still separate partitions

    # A dynamic guide alice cached is never served to bob (no study_alpha), and
    # her cache entry is not even looked up under bob's partition.
    monkeypatch.setattr(server, "current_surface_problem", lambda: None)
    monkeypatch.setattr(server, "_resolve_study_guide_name", lambda _s: None)
    server._clear_study_guide_cache()
    server._cache_put(server._study_guide_cache, (alice, "study_alpha"), ("study_alpha", "ALICE"))
    with as_caller(BOB_TOKEN), patch.object(server, "run_select_query", return_value=[]):
        result = server._study_guide_text("study_alpha")
    assert "ALICE" not in result
    with as_caller(ALICE_TOKEN):
        assert server._study_guide_text("study_alpha") == "ALICE"
    # Revoked alice can't keep reading her cached guide after the TTL.
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS", "0")
    portal_dat.reset_for_tests()
    portal.revoke_user("alice@example.org")
    with as_caller(ALICE_TOKEN):
        assert server._study_guide_text("study_alpha").startswith("Error")
    server._clear_study_guide_cache()


def test_raw_token_never_in_logs_exceptions_or_telemetry(monkeypatch, portal, caplog):
    """Success and failure paths, with DEBUG logging everywhere (httpx included)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cbioportal_mcp import telemetry

    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen: list[str] = []

    def record(*args, **kwargs):
        seen.append(repr((args, kwargs)))

    def scenario(token):
        with as_caller(token):
            try:
                get_current_study_access()
            except AuthorizationError as exc:
                seen.append(str(exc))
                seen.append(repr(exc.__cause__) + repr(exc.__context__))
            ctx = telemetry._resolve_caller_context(type("C", (), {"fastmcp_context": None})())
            span = provider.get_tracer("t").start_span("x")
            telemetry._tag_caller_attributes(span, ctx)
            span.end()
            telemetry._llmobs_tool_span  # noqa: B018
            seen.append(repr(ctx))

    with patch.object(telemetry, "_emit_tool_metrics", side_effect=record):
        scenario(ALICE_TOKEN)  # success
        portal.revoke_user("alice@example.org")
        portal_dat.reset_for_tests()
        scenario(ALICE_TOKEN)  # revoked
        portal.outage = True
        portal_dat.reset_for_tests()
        scenario(BOB_TOKEN)  # outage
        portal.outage = False

        def raise_(request):
            raise httpx.ConnectError(f"connect failed {request.headers}")

        monkeypatch.setattr(portal_dat, "_transport_for_tests", httpx.MockTransport(raise_))
        portal_dat.reset_for_tests()
        scenario(BOB_TOKEN)  # transport error whose message embeds headers

    texts = [r.getMessage() for r in caplog.records] + [
        str(r.exc_info) for r in caplog.records if r.exc_info
    ]
    texts += seen
    texts += [repr(dict(s.attributes)) for s in exporter.get_finished_spans()]
    texts += [repr(portal_dat._cache._entries)]
    for token in (ALICE_TOKEN, BOB_TOKEN):
        leaked = [t for t in texts if token in t]
        assert not leaked, leaked


def test_strict_gate_denies_without_token(portal):
    with as_caller(None), pytest.raises(AuthorizationError, match="x-auth-token"):
        resolve_caller_identity(strict=True)
    with as_caller(None):
        assert resolve_caller_identity().user_id is None  # telemetry never raises


def test_config_requires_https(monkeypatch, portal):
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_URL", "http://portal.example.org")
    with pytest.raises(ValueError, match="https"):
        portal_dat.portal_base_url(get_mcp_config())
    monkeypatch.setenv("CBIOPORTAL_MCP_PORTAL_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING", "true")
    assert portal_dat.portal_base_url(get_mcp_config()) == "http://portal.example.org"
