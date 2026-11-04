"""End-to-end test of the MCP's own OAuth identity path through the production provider.

CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY=oauth.

The provider is built by ``cbioportal_mcp.auth._build_auth_provider()`` - the
same ``GoogleProvider`` (FastMCP ``OAuthProxy``) production runs, with the
production Fernet-encrypted client/token storage wrapper (over an in-memory
store instead of Redis). Only Google's own HTTP endpoints are mocked: the
OAuth token endpoint, v1 tokeninfo and v2 userinfo. The test walks the real
OAuth flow against the real ``server.mcp`` app served by uvicorn:

1. dynamic client registration (``/register``);
2. ``/authorize`` -> redirect to Google (consent disabled, as in production);
3. Google's redirect back to ``/auth/callback`` with a code -> the proxy
   exchanges it at Google's token endpoint and stores the upstream token;
4. ``/token`` with PKCE -> an MCP-issued JWT;
5. MCP tool calls with that JWT: FastMCP verifies the JWT, looks the upstream
   token up via its jti, validates it with Google (tokeninfo/userinfo), and
   only then does the cbioportal identity code read the claims.

Checks: a verified Google account's email reaches ClickHouse as
SQL_cbio_user and its studies come back; an unverified Google account,
spoofed identity headers, a raw (non-MCP-issued) Google token, an unknown
token and no token get nothing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import oauth_proxy
from fastmcp.server.auth.providers import google as google_provider
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from cbioportal_mcp import auth as mcp_auth
from cbioportal_mcp import clickhouse_query_settings as cqs
from cbioportal_mcp import server

ENTITLEMENTS = {"alice@example.org": ["study_alpha", "study_public"]}
# Google's side: authorization code -> upstream access token ->
# (email, verified_email exactly as Google returns it).
GOOGLE_CODES = {"code-alice": "ya29.alice", "code-mallory": "ya29.mallory"}
GOOGLE_ACCOUNTS = {
    "ya29.alice": ("Alice@Example.org", True),
    "ya29.mallory": ("mallory@example.org", False),
}
CLIENT_REDIRECT = "http://127.0.0.1:65530/callback"


def _google_handler(request: httpx.Request) -> httpx.Response:
    """Google's OAuth token endpoint, v1 tokeninfo and v2 userinfo."""
    url = request.url
    if url.host == "oauth2.googleapis.com" and url.path == "/token":
        form = parse_qs(request.content.decode())
        upstream = GOOGLE_CODES.get(form.get("code", [""])[0])
        if not upstream:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": upstream,
                "expires_in": 3599,
                "token_type": "Bearer",
                "scope": "openid email profile",
            },
        )
    if url.path == "/oauth2/v1/tokeninfo":
        token = url.params.get("access_token")
    else:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token not in GOOGLE_ACCOUNTS:
        return httpx.Response(400, json={"error": "invalid_token"})
    email, verified = GOOGLE_ACCOUNTS[token]
    if url.path == "/oauth2/v1/tokeninfo":
        return httpx.Response(
            200,
            json={
                "audience": "google-client-id",
                "user_id": f"uid-{token}",
                "scope": "openid email profile",
                "expires_in": 3599,
                "email": email,
                "verified_email": verified,
            },
        )
    if url.path == "/oauth2/v2/userinfo":
        return httpx.Response(
            200, json={"id": f"uid-{token}", "email": email, "verified_email": verified}
        )
    return httpx.Response(404)


class FakeClickHouse:
    """Rows depend only on the identity setting, like the authorities policies."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, settings):
        self.calls.append((query, dict(settings)))
        visible = ENTITLEMENTS.get(settings.get("SQL_cbio_user"), [])
        if "FROM user_study" in query:
            return {"columns": ["cancer_study_identifier"], "rows": [[s] for s in visible]}
        cols = ["cancer_study_identifier", "name", "description", "type_of_cancer_id"]
        rows = [[s, s, "", "brca", 1] for s in visible]
        return {"columns": cols + ["sample_count"], "rows": rows}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def _served():
    """One uvicorn server for the module (server.mcp's app is started once per process)."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
        mp.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "authorities")
        mp.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY", "oauth")
        mp.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
        mp.setenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", "google-client-id")
        mp.setenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET", "google-client-secret")
        mp.setenv("CBIOPORTAL_MCP_GOOGLE_BASE_URL", base_url)
        mp.setenv("REDIS_URL", "redis://unused-in-test:6379/1")
        for name in ("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING"):
            mp.delenv(name, raising=False)

        # Production's encrypted storage wrapper, over memory instead of Redis.
        mp.setattr(
            mcp_auth,
            "_build_client_storage",
            lambda secret, _url: FernetEncryptionWrapper(
                key_value=MemoryStore(),
                fernet=Fernet(key=mcp_auth._derive_storage_encryption_key(secret)),
            ),
        )
        # Only Google's endpoints are mocked: the token verifier's httpx client
        # and the proxy's authlib client (an httpx.AsyncClient subclass).
        transport = httpx.MockTransport(_google_handler)
        real_async_client = httpx.AsyncClient

        def google_client(*args, **kwargs):
            return real_async_client(*args, transport=transport, **kwargs)

        mp.setattr(
            google_provider,
            "httpx",
            SimpleNamespace(AsyncClient=google_client, RequestError=httpx.RequestError),
        )
        real_oauth_client = oauth_proxy.AsyncOAuth2Client

        class GoogleTokenEndpointClient(real_oauth_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        mp.setattr(oauth_proxy, "AsyncOAuth2Client", GoogleTokenEndpointClient)

        fake = FakeClickHouse()
        mp.setattr(cqs, "run_query_with_settings", fake)
        mp.setattr(server, "run_query_with_settings", fake)
        mp.setattr(cqs, "current_surface_problem", lambda: None)
        mp.setattr(server, "current_surface_problem", lambda: None)
        mp.setattr(server, "_list_available_study_guides", lambda: [])

        provider = mcp_auth._build_auth_provider()
        assert isinstance(provider, google_provider.GoogleProvider)
        mp.setattr(server.mcp, "auth", provider)

        app = server.mcp.http_app(path="/mcp")
        uv_server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        )
        thread = threading.Thread(target=uv_server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not uv_server.started:
            if time.monotonic() > deadline:
                pytest.fail("uvicorn did not start")
            time.sleep(0.05)
        try:
            yield base_url, fake
        finally:
            uv_server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture
def oauth_server(_served):
    base_url, fake = _served
    fake.calls.clear()
    return base_url, fake


async def _mcp_issued_token(base_url: str, google_code: str) -> str:
    """Run the real OAuth flow through the proxy; return the MCP-issued access token."""
    async with httpx.AsyncClient(base_url=base_url, timeout=20) as http:
        registered = await http.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "oauth-identity-integration-test",
            },
        )
        assert registered.status_code in (200, 201), registered.text
        client_id = registered.json()["client_id"]

        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        authorize = await http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "client-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert authorize.status_code in (302, 303, 307), authorize.text
        to_google = urlparse(authorize.headers["location"])
        assert to_google.netloc == "accounts.google.com", authorize.headers["location"]
        txn_state = parse_qs(to_google.query)["state"][0]

        # Google redirects the browser back to the proxy with its own code.
        callback = await http.get(
            "/auth/callback", params={"code": google_code, "state": txn_state}
        )
        assert callback.status_code in (302, 303, 307), callback.text
        location = callback.headers["location"]
        assert location.startswith(CLIENT_REDIRECT), location
        client_query = parse_qs(urlparse(location).query)
        assert client_query["state"] == ["client-state"]

        token = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": client_query["code"][0],
                "redirect_uri": CLIENT_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        access_token = token.json()["access_token"]
    # It is the proxy's own token, never Google's.
    assert access_token not in GOOGLE_ACCOUNTS
    return access_token


async def _call(base_url: str, tool: str, token: str | None, headers: dict | None = None):
    all_headers = dict(headers or {})
    if token:
        all_headers["authorization"] = f"Bearer {token}"
    transport = StreamableHttpTransport(f"{base_url}/mcp/", headers=all_headers)
    async with Client(transport) as client:
        result = await client.call_tool(tool, {}, raise_on_error=False)
    return result.structured_content or {}


def test_verified_google_identity_reaches_clickhouse_through_the_provider(oauth_server):
    base_url, fake = oauth_server
    token = asyncio.run(_mcp_issued_token(base_url, "code-alice"))
    access = asyncio.run(_call(base_url, "get_current_study_access", token))
    assert access.get("user_email") == "alice@example.org", access
    assert access["user_id"] == "uid-ya29.alice"
    assert set(access["studies"]) == {"study_alpha", "study_public"}
    assert fake.calls and all(s == {"SQL_cbio_user": "alice@example.org"} for _q, s in fake.calls)

    studies = asyncio.run(_call(base_url, "list_studies", token))["result"]
    assert {row["cancer_study_identifier"] for row in studies} == {"study_alpha", "study_public"}


def test_unverified_google_email_is_denied_through_the_provider(oauth_server):
    base_url, fake = oauth_server
    token = asyncio.run(_mcp_issued_token(base_url, "code-mallory"))
    access = asyncio.run(_call(base_url, "get_current_study_access", token))
    assert "verified email" in access.get("error_message", ""), access
    assert fake.calls == []


def test_identity_headers_do_not_replace_the_token(oauth_server):
    base_url, fake = oauth_server
    token = asyncio.run(_mcp_issued_token(base_url, "code-mallory"))
    spoof = {"x-user-email": "alice@example.org", "x-user-id": "alice"}
    access = asyncio.run(_call(base_url, "get_current_study_access", token, spoof))
    assert "error_message" in access, access
    assert fake.calls == []


@pytest.mark.parametrize("token", [None, "not-a-token", "ya29.alice"])
def test_requests_without_an_mcp_issued_token_are_rejected(oauth_server, token):
    # "ya29.alice" is a valid *Google* token: the proxy only accepts its own JWTs.
    base_url, fake = oauth_server
    with pytest.raises(Exception, match="401|Unauthorized|invalid_token"):
        asyncio.run(_call(base_url, "get_current_study_access", token))
    assert fake.calls == []
