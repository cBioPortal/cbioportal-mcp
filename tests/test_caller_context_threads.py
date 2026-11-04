"""resolve_caller_identity() keeps working on server._submit_in_context workers.

get_study_guide fans its section queries out to a ThreadPoolExecutor; every
query runs the study-access checks, which read the caller through
resolve_caller_identity(). Its inputs - the HTTP request (headers such as
x-auth-token) and the validated access token - live in contextvars, which
pool threads only see when the submission carries a copy of the caller's
context. These tests set the real FastMCP request contextvar (no patching of
get_http_headers/get_access_token) and check the worker sees the same caller
for every provider, and that a bare executor.submit would not.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.http import _current_http_request
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.requests import Request
from test_portal_dat import ALICE_TOKEN, portal  # noqa: F401 - pytest fixture

from cbioportal_mcp import server
from cbioportal_mcp.authentication.caller import resolve_caller_identity
from cbioportal_mcp.authentication.study_access import AuthorizationError

OIDC_ENV = {
    "CBIOPORTAL_MCP_AUTH_PROVIDER": "oidc",
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE": "restricted",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE": "jwt",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "true",
    "CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM": "cbio_studies",
}


def _request(headers: dict, claims: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    if claims is not None:
        token = AccessToken(
            token="opaque",
            client_id="c",
            scopes=[],
            expires_at=int(time.time()) + 300,
            claims=claims,
        )
        scope["user"] = AuthenticatedUser(token)
    return Request(scope)


def _in_request(request: Request, fn):
    """Run fn on a pool thread, once with _submit_in_context and once bare."""
    reset = _current_http_request.set(request)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            propagated = server._submit_in_context(pool, fn)
            bare = pool.submit(fn)
            return propagated.result(), bare
    finally:
        _current_http_request.reset(reset)


def _env(monkeypatch, values: dict) -> None:
    for name in (
        "CBIOPORTAL_MCP_AUTH_PROVIDER",
        "CBIOPORTAL_MCP_STUDY_ACCESS_MODE",
        "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE",
        "CBIOPORTAL_MCP_STUDY_ACL_FILE",
        "CBIOPORTAL_MCP_ALLOWED_STUDIES",
        "CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_google_identity_reaches_worker_threads(monkeypatch):
    _env(monkeypatch, {})
    request = _request({}, {"sub": "g-123", "email": "alice@example.org"})
    identity, bare = _in_request(request, resolve_caller_identity)
    assert (identity.user_id, identity.email, identity.client_kind) == (
        "g-123",
        "alice@example.org",
        "oauth",
    )
    # Without the copied context the worker sees no caller at all.
    assert bare.result().user_id is None


def test_oidc_jwt_studies_reach_worker_threads(monkeypatch):
    _env(monkeypatch, OIDC_ENV)
    claims = {"sub": "u-1", "email": "a@example.org", "cbio_studies": ["study_alpha"]}
    identity, bare = _in_request(
        _request({}, claims), lambda: resolve_caller_identity(include_studies=True)
    )
    assert identity.user_id == "u-1"
    assert identity.studies == frozenset({"study_alpha"})
    with pytest.raises(AuthorizationError):
        bare.result()  # fails closed, never falls back to another identity


def test_portal_dat_token_reaches_worker_threads(portal):  # noqa: F811
    identity, bare = _in_request(
        _request({"x-auth-token": ALICE_TOKEN}),
        lambda: resolve_caller_identity(include_studies=True),
    )
    assert identity.client_kind == "portal_dat"
    assert identity.studies == frozenset({"study_alpha", "study_public"})
    with pytest.raises(AuthorizationError):
        bare.result()


def test_study_access_settings_on_worker_match_the_caller(portal):  # noqa: F811
    """What run_select_query sends to ClickHouse from a guide worker."""
    from cbioportal_mcp.authentication.study_access import clickhouse_study_access_settings

    settings, _bare = _in_request(
        _request({"x-auth-token": ALICE_TOKEN}), clickhouse_study_access_settings
    )
    assert settings == {"SQL_cbiomcp_allowed_studies": "study_alpha,study_public"}
