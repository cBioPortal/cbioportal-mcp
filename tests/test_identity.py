"""Admission contract at the identity, transport, and MCP handler boundaries."""

import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from cbioportal_mcp import rollout
from cbioportal_mcp.env import validate_startup
from cbioportal_mcp.identity import (
    BRIDGE_HEADERS,
    CombinedAuth,
    IdentityMiddleware,
    google_identity,
    request_decision,
)
from cbioportal_mcp.server import ConfiguredFastMCP
from cbioportal_mcp.telemetry import _resolve_caller_identity

SECRET = "unit-test-bridge-secret"
GOOGLE_ENV = {
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "test-client",
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": "test-secret",
    "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.example.org",
}


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    for name in GOOGLE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CBIOPORTAL_MCP_LOCAL_DEV", raising=False)
    monkeypatch.delenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", raising=False)
    monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", SECRET)
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "enforce")
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, "")


def pin():
    validate_startup()


def bridge(**updates):
    return {
        "x-user-email": "alice@example.org",
        "x-user-id": "bridge-id",
        "x-user-email-verified": "true",
        "x-cbioportal-mcp-bridge-secret": SECRET,
        **updates,
    }


def token(evidence=None, **claims):
    return AccessToken(
        token="validated-upstream-token",
        client_id="test-client",
        scopes=[],
        claims={
            "sub": "google-id",
            "email": "alice@example.org",
            "google_user_data": {"verified_email": True} if evidence is None else evidence,
            **claims,
        },
    )


def google(result=None):
    return SimpleNamespace(verify_token=AsyncMock(return_value=result))


@pytest.mark.parametrize("field", ["google_user_data", "google_token_info"])
@pytest.mark.parametrize("key", ["verified_email", "email_verified"])
@pytest.mark.parametrize(
    "value,valid",
    [
        (True, True),
        ("true", True),
        (False, False),
        (None, False),
        ("false", False),
        ("unknown", False),
        (1, False),
        ("${VERIFIED}", False),
    ],
)
def test_nested_google_verification(field, key, value, valid):
    claims = {"google_user_data": {}, field: {key: value}}
    assert (google_identity(token(**claims)) is not None) == valid


def test_google_requires_nested_evidence_and_identity():
    for claims in (
        {"google_user_data": {}, "email_verified": True},
        {"email": None},
        {"sub": None},
        {"google_token_info": {"verified_email": False}},
    ):
        assert google_identity(token(**claims)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("x-cbioportal-mcp-bridge-secret", ""),
        ("x-cbioportal-mcp-bridge-secret", "wrong"),
        ("x-cbioportal-mcp-bridge-secret", "${CBIOPORTAL_MCP_BRIDGE_SECRET}"),
        ("x-cbioportal-mcp-bridge-secret", "{{BRIDGE_SECRET}}"),
        ("x-user-email-verified", "${VERIFIED}"),
        ("x-user-email-verified", "{{VERIFIED}}"),
        ("x-user-email-verified", "false"),
        ("x-user-email-verified", "unknown"),
        ("x-user-email-verified", ""),
        ("x-user-email-verified", "1"),
        ("x-user-id", ""),
        ("x-user-email", "invalid"),
    ],
)
async def test_bridge_rejects_invalid_fields(field, value):
    pin()
    decision = await CombinedAuth().decide(Headers(bridge(**{field: value})))
    assert decision.checked and not decision.allowed and decision.identity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(BRIDGE_HEADERS))
async def test_bridge_requires_all_headers(field):
    pin()
    headers = bridge()
    del headers[field]
    assert not (await CombinedAuth().decide(Headers(headers))).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["", " ", "${SECRET}", "{{SECRET}}"])
async def test_bridge_cannot_trust_unconfigured_or_placeholder_secret(monkeypatch, configured):
    pin()
    monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", configured)
    headers = bridge(**{"x-cbioportal-mcp-bridge-secret": configured})
    assert not (await CombinedAuth().decide(Headers(headers))).allowed


@pytest.mark.asyncio
async def test_compare_is_constant_time_and_config_is_captured(monkeypatch):
    pin()
    auth = CombinedAuth()
    monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", "changed-after-construction")
    with patch(
        "cbioportal_mcp.identity.hmac.compare_digest", wraps=__import__("hmac").compare_digest
    ) as compare:
        decision = await auth.decide(Headers(bridge()))
    compare.assert_called_once_with(SECRET.encode(), SECRET.encode())
    assert decision.allowed and decision.identity.route == "librechat"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,email,verified,allowed,checked",
    [
        ("enforce", None, False, False, True),
        ("enforce", "other@example.org", False, False, True),
        ("enforce", "other@example.org", True, True, True),
        ("off", None, False, True, False),
        ("off", "other@example.org", False, True, False),
        ("off", " ALICE@EXAMPLE.ORG ", False, False, True),
        ("off", " ALICE@EXAMPLE.ORG ", True, True, True),
    ],
)
@pytest.mark.parametrize("route", ["oauth", "librechat"])
async def test_rollout_targeting(monkeypatch, mode, email, verified, allowed, checked, route):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, "Alice@Example.org")
    pin()
    provider = google()
    headers = {}
    if email:
        if route == "oauth":
            provider.verify_token.return_value = token({"verified_email": verified}, email=email)
            headers = {"authorization": "Bearer test-token"}
        else:
            headers = bridge(**{"x-user-email": email, "x-user-email-verified": str(verified)})
    if route == "librechat":
        provider = None
    elif email is None and mode == "off":
        allowed = False  # Google already required login before the rollout.
    decision = await CombinedAuth(provider).decide(Headers(headers))
    assert (decision.allowed, decision.checked) == (allowed, checked)
    if decision.identity:
        assert decision.identity.email == email.strip().lower()
    else:
        assert not verified or not email


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_route", ["oauth", "librechat"])
@pytest.mark.parametrize("mode", ["enforce", "off"])
async def test_no_fallback_from_invalid_credential(monkeypatch, bad_route, mode):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, "")
    pin()
    headers = {**bridge(), "authorization": "Bearer test-token"}
    provider = google(token())
    if bad_route == "oauth":
        provider.verify_token.return_value = None
        del headers["x-user-email"]  # No tester hint is needed to fail closed.
    else:
        headers["x-cbioportal-mcp-bridge-secret"] = "wrong"
    decision = await CombinedAuth(provider).decide(Headers(headers))
    assert not decision.allowed and decision.identity is None


@pytest.mark.asyncio
async def test_both_routes_coexist_and_invalid_credentials_always_reject(monkeypatch):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "off")
    pin()
    auth = CombinedAuth(google(token()))
    for headers, route in [
        (bridge(), "librechat"),
        ({"authorization": "Bearer t"}, "oauth"),
        ({**bridge(), "authorization": "Bearer t"}, "oauth"),
    ]:
        decision = await auth.decide(Headers(headers))
        assert decision.allowed and decision.identity.route == route
    decision = await auth.decide(Headers(bridge(**{"x-cbioportal-mcp-bridge-secret": "bad"})))
    assert not decision.allowed and decision.identity is None


@pytest.mark.asyncio
async def test_spoofed_or_duplicate_tester_email_only_adds_checks(monkeypatch):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "off")
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, "alice@example.org")
    pin()
    headers = Headers(
        raw=[(b"x-user-email", b"other@example.org"), (b"x-user-email", b"ALICE@EXAMPLE.ORG")]
    )
    assert not (await CombinedAuth().decide(headers)).allowed
    encoded = "b64:" + base64.b64encode(b"ALICE@EXAMPLE.ORG").decode()
    decision = await CombinedAuth().decide(Headers({"x-user-email": encoded}))
    assert decision.checked and not decision.allowed


@pytest.mark.asyncio
async def test_decision_logs_have_no_personal_data_or_secrets(caplog):
    pin()
    with caplog.at_level(logging.INFO, logger="cbioportal_mcp.identity"):
        await CombinedAuth(google(token())).decide(Headers(bridge()))
        await CombinedAuth().decide(Headers(bridge(**{"x-user-email-verified": "false"})))
        provider = google()
        provider.verify_token.side_effect = RuntimeError("alice@example.org " + SECRET)
        await CombinedAuth(provider).decide(Headers({"authorization": "Bearer test-token"}))
    assert "allowed=True" in caplog.text and "allowed=False" in caplog.text
    for value in ("alice@example.org", "bridge-id", SECRET, "test-token"):
        assert value not in caplog.text


@pytest.mark.parametrize("route", ["none", "bridge", "google"])
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_startup_requires_route_under_enforcement(monkeypatch, route, mode):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    monkeypatch.delenv("CBIOPORTAL_MCP_BRIDGE_SECRET")
    if route == "bridge":
        monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", SECRET)
    if route == "google":
        for name, value in GOOGLE_ENV.items():
            monkeypatch.setenv(name, value)
    if route == "none" and mode == "enforce":
        with pytest.raises(ValueError, match="Auth enforce requires"):
            pin()
    else:
        pin()


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("missing", list(GOOGLE_ENV))
def test_partial_google_rejected_even_with_bridge(monkeypatch, mode, missing):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    for name, value in GOOGLE_ENV.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="Partial Google OAuth"):
        pin()


@pytest.mark.parametrize("secret", [" ", "${CBIOPORTAL_MCP_BRIDGE_SECRET}", "{{BRIDGE}}"])
def test_startup_rejects_unexpanded_secret(monkeypatch, secret):
    monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", secret)
    with pytest.raises(ValueError, match="must be non-empty and expanded") as exc:
        pin()
    assert secret.strip() not in str(exc.value) or not secret.strip()


@pytest.mark.parametrize(
    "name",
    [
        "CBIOPORTAL_MCP_AUTH_REQUIRED",
        "ALLOW_UNAUTHENTICATED",
        "CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED",
    ],
)
@pytest.mark.asyncio
async def test_legacy_bypass_flags_have_no_effect(monkeypatch, name):
    monkeypatch.setenv(name, "false" if "REQUIRED" in name else "true")
    pin()
    assert not (await CombinedAuth().decide(Headers())).allowed


METHODS = [
    ("tools/list", {}),
    ("resources/list", {}),
    ("resources/templates/list", {}),
    ("prompts/list", {}),
    ("tools/call", {"name": "probe"}),
    ("resources/read", {"uri": "test://probe"}),
    ("prompts/get", {"name": "probe"}),
    ("resources/read", {"uri": "test://template/value"}),
]


def make_server(provider=None):
    server = ConfiguredFastMCP(name="identity-test", auth=provider)
    calls = []

    def observed():
        calls.append((request_decision(), _resolve_caller_identity()))
        from fastmcp.server.dependencies import get_http_headers

        assert not BRIDGE_HEADERS.intersection(get_http_headers(include_all=True))
        return "ok"

    server.tool(name="probe")(observed)
    server.resource("test://probe")(observed)
    server.prompt(name="probe")(observed)

    @server.resource("test://template/{value}")
    def template(value: str):
        return observed()

    return server, calls


def rpc(client, method, params=None, headers=None, path="/mcp"):
    return client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
    )


@pytest.mark.parametrize("method,params", METHODS)
def test_every_operation_gated_before_handler_and_cache(method, params):
    server, calls = make_server()
    cache = Mock()

    class CachedMiddleware(Middleware):
        async def on_message(self, context, call_next):
            cache()
            return await call_next(context)

    server.add_middleware(CachedMiddleware())
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        response = rpc(client, method, params)
        assert response.status_code == 401
        assert "Bearer" in response.headers["www-authenticate"]
        assert not calls and not cache.called
        good = rpc(client, method, params, bridge())
        assert good.status_code == 200 and "error" not in good.json(), good.text
        assert cache.called
        admitted_cache_calls = cache.call_count
        response = rpc(client, method, params, bridge(**{"x-user-email-verified": "false"}))
        assert response.status_code == 401
        assert cache.call_count == admitted_cache_calls
    if calls:
        assert calls[0][1] == ("bridge-id", "alice@example.org", "librechat")


def test_http_decides_once_per_request_and_oauth_routes_remain_public():
    async def metadata(request):
        return JSONResponse({"issuer": "https://mcp.example.org"})

    provider = google(token())
    provider.get_routes = Mock(return_value=[Route("/.well-known/test", metadata)])
    provider._get_resource_url = Mock(return_value="https://mcp.example.org/mcp")
    server, calls = make_server(provider)
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        assert client.get("/.well-known/test").status_code == 200
        good = rpc(client, "tools/call", {"name": "probe"}, {"authorization": "Bearer t"})
        assert good.status_code == 200 and "error" not in good.json(), good.text
        provider.verify_token.assert_awaited_once_with("t")
        assert calls[0][1] == ("google-id", "alice@example.org", "oauth")
        denied = rpc(client, "tools/list")
        assert denied.status_code == 401
        assert (
            'resource_metadata="https://mcp.example.org/.well-known/oauth-protected-resource/mcp"'
            in denied.headers["www-authenticate"]
        )
    provider.get_routes.assert_called_once_with(mcp_path="/mcp")


def test_existing_session_rechecks_each_request():
    server, calls = make_server()
    with TestClient(server.http_app(json_response=True)) as client:
        init = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            bridge(),
        )
        assert init.status_code == 200
        session = {"mcp-session-id": init.headers["mcp-session-id"]}
        assert rpc(client, "tools/list", headers=session).status_code == 401
        changed = bridge(**{"x-user-email": "bob@example.org", "x-user-id": "second-id"})
        response = rpc(client, "tools/call", {"name": "probe"}, {**changed, **session})
        assert response.status_code == 200 and "error" not in response.json(), response.text
        assert calls[0][1] == ("second-id", "bob@example.org", "librechat")


@pytest.mark.parametrize(
    "transport,path", [("http", "/custom"), ("sse", "/events"), ("sse", "/messages/")]
)
def test_transport_endpoints_are_protected(transport, path):
    server, _ = make_server()
    app = server.http_app(transport=transport, path="/events" if transport == "sse" else path)
    with TestClient(app) as client:
        response = (
            client.get(path) if path != "/messages/" else rpc(client, "tools/list", path=path)
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_boundary_rejects_without_http_admission():
    pin()
    server = SimpleNamespace(startup_transport="http")
    handler = AsyncMock()
    for method, params in METHODS:
        with pytest.raises(ToolError, match="Verified login required"):
            await IdentityMiddleware(server)(
                MiddlewareContext(method=method, message=params), handler
            )
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_stdio_local_only_never_carries_identity(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    with pytest.raises(ValueError, match="LOCAL_DEV=1"):
        pin()
    monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", "1")
    pin()
    server = SimpleNamespace(startup_transport="stdio")

    async def handler(context):
        assert request_decision().identity is None
        assert _resolve_caller_identity() == (None, None, "direct")

    with patch("fastmcp.server.dependencies.get_access_token", return_value=token()):
        await IdentityMiddleware(server)(
            MiddlewareContext(method="tools/call", message={}), handler
        )
    assert request_decision() is None


def test_exempt_http_identity_headers_are_ignored(monkeypatch):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "off")
    server, calls = make_server()
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        response = rpc(
            client,
            "tools/call",
            {"name": "probe"},
            {
                "x-user-email": "spoof@example.org",
                "x-user-id": "spoof-id",
            },
        )
        assert response.status_code == 200 and "error" not in response.json()
    assert calls[0][0].identity is None
    assert calls[0][1] == (None, None, "direct")


def test_deprecated_sse_launcher_protected():
    server, _ = make_server()
    with TestClient(
        server.sse_app(path="/legacy-events", message_path="/legacy-messages/")
    ) as client:
        assert client.get("/legacy-events").status_code == 401
        assert rpc(client, "tools/list", path="/legacy-messages/").status_code == 401


@pytest.mark.parametrize("launcher", ["construct", "run", "stdio", "http", "sse", "lifespan"])
@pytest.mark.asyncio
async def test_all_launch_paths_revalidate_auth_configuration(monkeypatch, launcher):
    if launcher == "stdio":
        monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
        monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", "1")
    server, _ = make_server()
    app = server.http_app() if launcher == "lifespan" else None
    monkeypatch.setenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", "partial-client")
    with pytest.raises(ValueError, match="Partial Google OAuth"):
        if launcher == "construct":
            ConfiguredFastMCP(name="partial")
        elif launcher == "run":
            await server.run_async()
        elif launcher == "stdio":
            await server.run_stdio_async()
        elif launcher == "http":
            server.http_app()
        elif launcher == "sse":
            server.sse_app()
        else:
            async with app.router.lifespan_context(app):
                pass


@pytest.mark.asyncio
async def test_real_fastmcp_google_verifier_claim_layout():
    from fastmcp.server.auth.providers.google import GoogleTokenVerifier

    verifier = GoogleTokenVerifier(required_scopes=["openid", "email", "profile"])
    api = AsyncMock()
    api.get.side_effect = [
        Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "scope": "openid email profile",
                    "expires_in": 3600,
                    "user_id": "google-id",
                }
            ),
        ),
        Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "id": "google-id",
                    "email": "alice@example.org",
                    "verified_email": True,
                }
            ),
        ),
    ]
    with patch("fastmcp.server.auth.providers.google.httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value = api
        validated = await verifier.verify_token("test-google-access-token")
    assert "email_verified" not in validated.claims
    assert validated.claims["google_user_data"]["verified_email"] is True
    assert google_identity(validated).email == "alice@example.org"


@pytest.mark.parametrize("evidence", [{}, {"verified_email": None}, "unknown", [True]])
def test_unknown_google_evidence_fails_closed(evidence):
    assert google_identity(token(evidence)) is None


def test_real_google_discovery_and_registration_survive_adapter():
    from importlib.metadata import version

    # Deliberate upgrade tripwire: the adapter uses this version's private API.
    assert version("fastmcp") == "2.13.3", "Revalidate CombinedAuth's _get_resource_url adapter"
    from fastmcp.server.auth.providers.google import GoogleProvider
    from key_value.aio.stores.memory import MemoryStore

    provider = GoogleProvider(
        client_id="test-client",
        client_secret="test-secret",
        base_url="https://mcp.example.org",
        client_storage=MemoryStore(),
        required_scopes=["openid", "email", "profile"],
        require_authorization_consent=False,
    )
    assert str(provider._get_resource_url("/mcp")) == "https://mcp.example.org/mcp"
    server, _ = make_server(provider)
    with TestClient(server.http_app(stateless_http=True)) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == "https://mcp.example.org/mcp"
        oauth = client.get("/.well-known/oauth-authorization-server")
        assert oauth.status_code == 200
        assert oauth.json()["authorization_endpoint"] == "https://mcp.example.org/authorize"
        registration = client.post(
            "/register",
            json={
                "client_name": "test-connector",
                "redirect_uris": ["https://connector.example.org/callback"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201, registration.text
        assert registration.json()["client_id"]
        denied = rpc(client, "tools/list")
        assert denied.status_code == 401
        assert "oauth-protected-resource/mcp" in denied.headers["www-authenticate"]


@pytest.mark.parametrize(
    "configured,headers,testers,result,status",
    [
        (True, {}, "", None, 401),
        (True, {"authorization": "Bearer garbage"}, "", None, 401),
        (True, {"authorization": "Bearer garbage"}, "alice@example.org", None, 401),
        (True, {"authorization": "Bearer valid"}, "", token(), 200),
        (True, {"authorization": "Bearer legacy"}, "", token({}), 200),
        (False, {}, "", None, 200),
        (False, {"authorization": "Bearer garbage"}, "", None, 401),
        (False, bridge(**{"x-cbioportal-mcp-bridge-secret": "wrong"}), "", None, 401),
        (False, bridge(**{"x-cbioportal-mcp-bridge-secret": "${SECRET}"}), "", None, 401),
        (True, bridge(**{"x-user-email-verified": "false"}), "", None, 200),
        (True, bridge(**{"x-user-email-verified": "false"}), "alice@example.org", None, 401),
        (False, bridge(**{"x-user-email-verified": "false"}), "", None, 200),
    ],
)
def test_off_preserves_login_and_rejects_failed_credentials(
    monkeypatch,
    configured,
    headers,
    testers,
    result,
    status,
):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "off")
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, testers)
    provider = None
    if configured:
        for name, value in GOOGLE_ENV.items():
            monkeypatch.setenv(name, value)
        provider = google(result)
        provider.get_routes = Mock(return_value=[])
        provider._get_resource_url = Mock(return_value="https://mcp.example.org/mcp")
    server, _ = make_server(provider)
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        response = rpc(client, "tools/list", headers=headers)
        assert response.status_code == status, response.text
        if status == 401:
            assert "Bearer" in response.headers["www-authenticate"]
        else:
            assert "error" not in response.json()


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.asyncio
async def test_conflicting_valid_credentials_reject(monkeypatch, mode):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    pin()
    auth = CombinedAuth(google(token(email="bob@example.org")))
    decision = await auth.decide(Headers({**bridge(), "authorization": "Bearer valid"}))
    assert not decision.allowed and decision.identity is None


@pytest.mark.asyncio
async def test_bridge_user_id_is_stripped():
    pin()
    decision = await CombinedAuth().decide(Headers(bridge(**{"x-user-id": "  bridge-id  "})))
    assert decision.allowed and decision.identity.user_id == "bridge-id"


@pytest.mark.parametrize("mode", ["enforce", "off"])
@pytest.mark.parametrize("mounted", [False, True])
def test_root_path_and_parent_mount_preserve_auth_challenge(monkeypatch, mode, mounted):
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, mode)
    monkeypatch.setenv(rollout.AUTH_TEST_USERS_ENV, "alice@example.org")
    server, calls = make_server()
    app = server.http_app(stateless_http=True, json_response=True)
    path = "/deployment/mcp"
    if mounted:
        app = Starlette(routes=[Mount("/portal", app=app)])
        path = "/deployment/portal/mcp"
    with TestClient(app, root_path="/deployment") as client:
        response = rpc(
            client,
            "tools/call",
            {"name": "probe"},
            bridge(**{"x-user-email-verified": "false"}),
            path=path,
        )
        assert response.status_code == 401
        assert "Bearer" in response.headers["www-authenticate"]
        assert not calls
