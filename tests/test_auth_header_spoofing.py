"""Client-supplied identity/group/allowlist headers must never be trusted."""

from unittest.mock import patch

import pytest
from starlette.requests import Request

from cbioportal_mcp import local_keycloak_proxy as proxy
from cbioportal_mcp.authentication.study_access import (
    authz_header_names,
    extract_request_identity,
    get_current_study_access,
)
from cbioportal_mcp.env import McpConfig

SPOOFED = {
    "x-keycloak-groups": "/cbioportal/admins",
    "x-keycloak-sub": "admin",
    "x-keycloak-email": "admin@example.org",
    "x-user-groups": "/cbioportal/admins",
    "x-auth-request-user": "admin",
    "x-auth-request-groups": "/cbioportal/admins",
    "x-forwarded-groups": "/cbioportal/admins",
    "x-user-id": "admin",
    "x-cbioportal-allowed-studies": "*",
    "x-cbioportal-mcp-proxy-secret": "guess",
}


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.encode(), v.encode()) for k, v in {**headers, "accept": "text/event-stream"}.items()]
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": raw})


def test_proxy_strips_every_spoofable_header_before_injecting():
    userinfo = {"sub": "alice-sub", "email": "alice@example.org", "groups": ["/research/alpha"]}

    headers = proxy._upstream_headers(_request(SPOOFED), userinfo)

    assert headers["x-user-id"] == "alice-sub"
    assert headers["x-user-email"] == "alice@example.org"
    assert headers["x-forwarded-groups"] == "/research/alpha"
    assert headers[proxy.PROXY_SECRET_HEADER] == proxy.PROXY_SECRET
    assert headers["accept"] == "text/event-stream"
    leaked = {name for name in SPOOFED if name in headers and headers[name] == SPOOFED[name]}
    assert not leaked, leaked


def test_proxy_strips_configured_custom_header_names(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS", "x-my-groups")
    monkeypatch.setenv("CBIOPORTAL_MCP_ALLOWED_STUDIES_HEADER", "x-my-studies")
    names = authz_header_names(McpConfig())
    assert {"x-my-groups", "x-my-studies", "x-keycloak-groups"} <= names


def test_mcp_reads_only_configured_identity_headers(monkeypatch):
    monkeypatch.delenv("CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS", raising=False)
    headers = {
        "x-user-id": "alice",
        "x-forwarded-groups": "/research/alpha",
        **{k: v for k, v in SPOOFED.items() if k not in {"x-user-id", "x-forwarded-groups"}},
    }
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=headers):
        identity = extract_request_identity(McpConfig())
    assert identity.user_id == "alice"
    assert identity.groups == {"/research/alpha"}


def test_spoofed_admin_group_header_does_not_grant_wildcard(monkeypatch, tmp_path):
    acl = tmp_path / "acl.json"
    acl.write_text('{"groups": {"/cbioportal/admins": ["*"], "/research/alpha": ["study_alpha"]}}')
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cret")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACL_FILE", str(acl))
    headers = {
        "x-cbioportal-mcp-proxy-secret": "s3cret",
        "x-user-id": "alice",
        "x-forwarded-groups": "/research/alpha",
        "x-keycloak-groups": "/cbioportal/admins",
        "x-user-groups": "/cbioportal/admins",
    }
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=headers):
        access = get_current_study_access(McpConfig())
    assert access.all_studies is False
    assert access.studies == {"study_alpha"}


@pytest.mark.parametrize(
    ("env", "header"),
    [
        ("CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS", "x-auth-request-groups"),
        ("CBIOPORTAL_MCP_AUTH_USER_ID_HEADERS", "x-auth-request-user"),
    ],
)
def test_other_proxies_can_opt_into_their_header_names(monkeypatch, env, header):
    monkeypatch.setenv(env, header)
    with patch(
        "fastmcp.server.dependencies.get_http_headers",
        return_value={header: "value-from-proxy"},
    ):
        identity = extract_request_identity(McpConfig())
    assert "value-from-proxy" in (identity.user_id, *identity.groups)
