"""The real MCP over HTTP, per login provider (cf. test_oauth_identity_integration).

Each module-scoped fixture starts ``server.main()`` in a subprocess
(tests/integration_mcp_launcher.py; only ClickHouse is faked) configured
purely through environment variables, plus the external party it talks to,
served over real HTTP from this process:

- portal_dat: a fake cBioPortal (local_e2e_fakes.FakePortal). Clients send
  their DAT in x-auth-token.
- oidc: an Okta-shaped mock issuer (local_e2e_fakes.MockOidcIssuer: login
  in ``sub``, ``uid`` user id, custom ``cbio_studies`` claim, no
  email_verified). The test runs the real OAuth flow against the MCP's
  OIDCProxy (dynamic registration, /authorize, IdP login, /auth/callback,
  /token) and calls tools with the token the MCP issued.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from cbioportal_mcp.local_e2e_fakes import (
    FakePortal,
    MockOidcIssuer,
    fake_portal_app,
    mock_oidc_app,
)

HERE = Path(__file__).resolve().parent
ALICE_DAT = "0f1e2d3c-aaaa-4bbb-8ccc-alice0000001"
BOB_DAT = "0f1e2d3c-bbbb-4bbb-8ccc-bob000000002"
HMAC_SECRET = "integration-hmac-secret-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_in_thread(app) -> tuple[str, uvicorn.Server]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def _clean_env() -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CBIOPORTAL_MCP_", "CLICKHOUSE_", "DD_", "OTEL_", "REDIS_"))
    }
    env["CBIOPORTAL_MCP_DD_METRICS_ENABLED"] = "false"
    return env


def _start_mcp(env: dict, tmp: Path) -> tuple[str, subprocess.Popen, Path]:
    port = _free_port()
    log = tmp / "clickhouse.jsonl"
    env = {
        **env,
        "CLICKHOUSE_MCP_SERVER_TRANSPORT": "http",
        "CLICKHOUSE_MCP_BIND_HOST": "127.0.0.1",
        "CLICKHOUSE_MCP_BIND_PORT": str(port),
        "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
        "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
        "FASTMCP_HOME": str(tmp / "fastmcp-home"),
        "INTEGRATION_CLICKHOUSE_LOG": str(log),
    }
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "integration_mcp_launcher.py")],
        env=env,
        cwd=HERE.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("MCP exited:\n" + proc.stdout.read().decode(errors="replace"))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return f"http://127.0.0.1:{port}", proc, log
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("MCP did not start")


def _stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out.decode(errors="replace")


async def _call(url: str, tool: str, headers: dict) -> dict:
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        result = await client.call_tool(tool, {}, raise_on_error=False)
    if result.is_error:
        return {"error": " ".join(getattr(c, "text", "") for c in result.content)}
    return result.structured_content or {}


def call(url: str, tool: str, headers: dict | None = None) -> dict:
    return asyncio.run(_call(url, tool, headers or {}))


# --- portal_dat ----------------------------------------------------------------


@pytest.fixture(scope="module")
def dat_stack(tmp_path_factory):
    portal = FakePortal()
    portal.add_user("alice@example.org", ["study_alpha", "study_public"], ALICE_DAT)
    portal.add_user("bob", ["study_beta"], BOB_DAT)
    portal_url, portal_server = _serve_in_thread(fake_portal_app(portal))
    env = {
        **_clean_env(),
        "CBIOPORTAL_MCP_AUTH_PROVIDER": "portal_dat",
        "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "portal_dat",
        "CBIOPORTAL_MCP_PORTAL_URL": portal_url,
        "CBIOPORTAL_MCP_PORTAL_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING": "true",
        "CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET": HMAC_SECRET,
        "CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF": "true",
        "CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS": "1",
    }
    base, proc, log = _start_mcp(env, tmp_path_factory.mktemp("dat"))
    try:
        yield base + "/mcp/", portal, log, proc
    finally:
        output = _stop(proc)
        portal_server.should_exit = True
        for token in (ALICE_DAT, BOB_DAT):
            assert token not in output, "raw DAT in server output"


def test_dat_studies_are_the_portal_list(dat_stack):
    url, _portal, log, _ = dat_stack
    access = call(url, "get_current_study_access", {"x-auth-token": ALICE_DAT})
    assert set(access["studies"]) == {"study_alpha", "study_public"}
    assert access["user_id"] == "portal:127.0.0.1:alice@example.org" or access["user_id"].endswith(
        ":alice@example.org"
    )
    studies = call(url, "list_studies", {"x-auth-token": BOB_DAT})["result"]
    assert [s["cancer_study_identifier"] for s in studies] == ["study_beta"]


def test_dat_missing_or_unknown_token_denied(dat_stack):
    url, *_ = dat_stack
    assert "x-auth-token" in call(url, "list_studies").get("error", "")
    assert "error" in call(url, "list_studies", {"x-auth-token": "not-a-real-token-0000"})


def test_dat_client_cannot_choose_the_portal_or_identity(dat_stack):
    url, *_ = dat_stack
    spoof = {
        "x-auth-token": BOB_DAT,
        "x-user-email": "alice@example.org",
        "x-cbioportal-allowed-studies": "study_alpha",
        "x-forwarded-host": "evil.example.org",
    }
    access = call(url, "get_current_study_access", spoof)
    assert set(access["studies"]) == {"study_beta"}


def test_dat_revocation_and_outage_deny(dat_stack):
    url, portal, *_ = dat_stack
    token = portal.add_user("carol", ["study_public"])
    assert call(url, "get_current_study_access", {"x-auth-token": token})["studies"] == [
        "study_public"
    ]
    portal.revoke_user("carol")
    time.sleep(1.2)  # cache TTL
    assert "error" in call(url, "get_current_study_access", {"x-auth-token": token})
    portal.outage = True
    try:
        time.sleep(1.2)
        assert "error" in call(url, "get_current_study_access", {"x-auth-token": ALICE_DAT})
    finally:
        portal.outage = False


def test_dat_only_reads_and_never_token_in_url(dat_stack):
    _url, portal, *_ = dat_stack
    assert portal.requests
    assert {method for method, _path, _auth in portal.requests} == {"GET"}
    assert all("/api/data-access-tokens/" not in path for _m, path, _a in portal.requests)
    assert all(ALICE_DAT not in path and BOB_DAT not in path for _m, path, _a in portal.requests)


# --- oidc (Okta-shaped mock issuer) ------------------------------------------------


@pytest.fixture(scope="module")
def oidc_stack(tmp_path_factory):
    idp_port = _free_port()
    issuer_url = f"http://127.0.0.1:{idp_port}/oauth2/default"
    issuer = MockOidcIssuer(issuer_url, "api://default")
    issuer.users.update(
        {
            "alice@example.org": {
                "uid": "00u-alice",
                "groups": ["Everyone"],
                "studies": ["study_alpha"],
            },
            "bob@example.org": {
                "uid": "00u-bob",
                "groups": [],
                "studies": ["study_beta", "study_public"],
            },
            "nostudies@example.org": {"uid": "00u-none"},
        }
    )
    app = mock_oidc_app(issuer)
    idp_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=idp_port, log_level="warning")
    )
    threading.Thread(target=idp_server.run, daemon=True).start()
    while not idp_server.started:
        time.sleep(0.05)

    mcp_port_env = {
        **_clean_env(),
        "CBIOPORTAL_MCP_AUTH_PROVIDER": "oidc",
        "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "jwt",
        "CBIOPORTAL_MCP_OIDC_ISSUER_URL": issuer_url,
        "CBIOPORTAL_MCP_OIDC_CLIENT_ID": "mock-client",
        "CBIOPORTAL_MCP_OIDC_CLIENT_SECRET": "mock-client-secret-0123456789abcdef",
        "CBIOPORTAL_MCP_OIDC_AUDIENCE": "api://default",
        "CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM": "uid",
        "CBIOPORTAL_MCP_OIDC_EMAIL_CLAIM": "sub",
        "CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM": "groups",
        "CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM": "cbio_studies",
        "CBIOPORTAL_MCP_OIDC_REQUIRE_CONSENT": "false",
        "CBIOPORTAL_MCP_OIDC_ALLOW_INSECURE_HTTP_FOR_LOCAL_TESTING": "true",
    }
    tmp = tmp_path_factory.mktemp("oidc")
    port = _free_port()
    env = {**mcp_port_env, "CBIOPORTAL_MCP_OIDC_BASE_URL": f"http://127.0.0.1:{port}"}
    # _start_mcp picks its own port; pin it to the one the base URL names.
    original = globals()["_free_port"]
    globals()["_free_port"] = lambda: port
    try:
        base, proc, log = _start_mcp(env, tmp)
    finally:
        globals()["_free_port"] = original
    try:
        yield base, issuer, proc
    finally:
        _stop(proc)
        idp_server.should_exit = True


def _login(base: str, issuer: MockOidcIssuer, login: str) -> str:
    """The full authorization-code flow a real MCP client runs, through OIDCProxy."""
    issuer.default_login = login
    redirect_uri = "http://127.0.0.1:9/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    with httpx.Client(follow_redirects=False, timeout=15) as http:
        reg = http.post(
            f"{base}/register",
            json={
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "integration-test",
            },
        )
        assert reg.status_code in (200, 201), reg.text
        client_id = reg.json()["client_id"]
        resp = http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "state": "s1",
            },
        )
        hops = 0
        while resp.status_code in (302, 303, 307) and hops < 6:
            location = resp.headers["location"]
            if location.startswith(redirect_uri):
                break
            resp = http.get(location)
            hops += 1
        location = resp.headers.get("location", "")
        assert location.startswith(redirect_uri), (resp.status_code, resp.text[:300])
        code = parse_qs(urlparse(location).query)["code"][0]
        token = http.post(
            f"{base}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        return token.json()["access_token"]


def test_oidc_okta_shaped_login_end_to_end(oidc_stack):
    base, issuer, _ = oidc_stack
    jwt = _login(base, issuer, "alice@example.org")
    access = call(base + "/mcp/", "get_current_study_access", {"authorization": f"Bearer {jwt}"})
    assert access["user_id"] == "00u-alice"
    assert access["studies"] == ["study_alpha"]
    studies = call(base + "/mcp/", "list_studies", {"authorization": f"Bearer {jwt}"})["result"]
    assert [s["cancer_study_identifier"] for s in studies] == ["study_alpha"]

    bob = _login(base, issuer, "bob@example.org")
    access = call(base + "/mcp/", "get_current_study_access", {"authorization": f"Bearer {bob}"})
    assert set(access["studies"]) == {"study_beta", "study_public"}


def test_oidc_token_without_studies_claim_denied(oidc_stack):
    base, issuer, _ = oidc_stack
    jwt = _login(base, issuer, "nostudies@example.org")
    access = call(base + "/mcp/", "get_current_study_access", {"authorization": f"Bearer {jwt}"})
    assert "study claim" in access.get("error_message", access.get("error", "")), access


@pytest.mark.parametrize("token", [None, "not-a-jwt"])
def test_oidc_requests_without_valid_token_rejected(oidc_stack, token):
    base, issuer, _ = oidc_stack
    headers = {"authorization": f"Bearer {token}"} if token else {}
    with pytest.raises(Exception, match="401|Unauthorized|invalid_token"):
        call(base + "/mcp/", "get_current_study_access", headers)


def test_oidc_raw_idp_token_is_not_accepted_directly(oidc_stack):
    """Only tokens the MCP issued after login are accepted, not upstream tokens."""
    base, issuer, _ = oidc_stack
    raw = issuer.access_token_for("alice@example.org")
    with pytest.raises(Exception, match="401|Unauthorized|invalid_token"):
        call(base + "/mcp/", "get_current_study_access", {"authorization": f"Bearer {raw}"})


def test_oidc_metadata_advertises_configured_base_url(oidc_stack):
    base, _issuer, _ = oidc_stack
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server", timeout=5).json()
    assert meta["authorization_endpoint"].startswith(base)
    assert json.dumps(meta).count("127.0.0.1") >= 1
