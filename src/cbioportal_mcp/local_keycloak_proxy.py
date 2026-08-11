"""Local-development Keycloak auth proxy for cBioPortal MCP e2e tests.

This proxy is intentionally small and intended for local e2e validation only.
Production deployments should use a hardened ingress/auth proxy that validates
Keycloak sessions or tokens, strips spoofable inbound headers, and injects the
trusted identity headers consumed by cBioPortal MCP.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM_MCP_URL = os.getenv("LOCAL_AUTH_PROXY_UPSTREAM_MCP_URL", "http://127.0.0.1:8000/mcp/")
KEYCLOAK_USERINFO_URL = os.getenv(
    "LOCAL_AUTH_PROXY_KEYCLOAK_USERINFO_URL",
    "http://127.0.0.1:8080/realms/cbioportal-mcp-local/protocol/openid-connect/userinfo",
)
KEYCLOAK_INTROSPECTION_URL = os.getenv("LOCAL_AUTH_PROXY_KEYCLOAK_INTROSPECTION_URL")
KEYCLOAK_INTROSPECTION_CLIENT_ID = os.getenv("LOCAL_AUTH_PROXY_KEYCLOAK_CLIENT_ID")
KEYCLOAK_INTROSPECTION_CLIENT_SECRET = os.getenv("LOCAL_AUTH_PROXY_KEYCLOAK_CLIENT_SECRET")
KEYCLOAK_INTROSPECTION_HOST_HEADER = os.getenv("LOCAL_AUTH_PROXY_KEYCLOAK_HOST_HEADER")
PROXY_SECRET = os.getenv("LOCAL_AUTH_PROXY_SECRET", "local-proxy-secret")
PROXY_SECRET_HEADER = os.getenv(
    "LOCAL_AUTH_PROXY_SECRET_HEADER",
    "x-cbioportal-mcp-proxy-secret",
)

STRIPPED_REQUEST_HEADERS = {
    "host",
    "authorization",
    "content-length",
    "x-user-id",
    "x-user-email",
    "x-user-groups",
    "x-auth-request-user",
    "x-auth-request-email",
    "x-auth-request-groups",
    "x-forwarded-user",
    "x-forwarded-email",
    "x-forwarded-groups",
    "x-cbioportal-allowed-studies",
    "x-cbioportal-mcp-proxy-secret",
}
STRIPPED_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
}


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def _load_userinfo(token: str) -> dict:
    client: httpx.AsyncClient = app.state.http_client
    if KEYCLOAK_INTROSPECTION_URL:
        headers = {}
        if KEYCLOAK_INTROSPECTION_HOST_HEADER:
            headers["host"] = KEYCLOAK_INTROSPECTION_HOST_HEADER
        response = await client.post(
            KEYCLOAK_INTROSPECTION_URL,
            headers=headers,
            data={
                "token": token,
                "client_id": KEYCLOAK_INTROSPECTION_CLIENT_ID or "",
                "client_secret": KEYCLOAK_INTROSPECTION_CLIENT_SECRET or "",
            },
        )
        if response.status_code != 200:
            raise PermissionError("Keycloak token introspection failed.")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("active"):
            raise PermissionError("Keycloak rejected the bearer token.")
        if not payload.get("sub"):
            raise PermissionError("Keycloak introspection response did not include a subject.")
        return payload

    response = await client.get(
        KEYCLOAK_USERINFO_URL,
        headers={"authorization": f"Bearer {token}"},
    )
    if response.status_code != 200:
        raise PermissionError("Keycloak rejected the bearer token.")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("sub"):
        raise PermissionError("Keycloak userinfo response did not include a subject.")
    return payload


def _header_value(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _upstream_headers(request: Request, userinfo: dict) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in STRIPPED_REQUEST_HEADERS
    }

    headers["x-user-id"] = _header_value(userinfo.get("sub", ""))
    if userinfo.get("email"):
        headers["x-user-email"] = _header_value(userinfo["email"])
    elif userinfo.get("preferred_username"):
        headers["x-user-email"] = _header_value(userinfo["preferred_username"])

    groups = userinfo.get("groups")
    if groups:
        headers["x-forwarded-groups"] = _header_value(groups)

    headers[PROXY_SECRET_HEADER] = PROXY_SECRET
    return headers


async def _stream_response(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk


async def proxy_mcp(request: Request) -> Response:
    token = _bearer_token(request)
    if token is None:
        return JSONResponse({"error": "Missing bearer token."}, status_code=401)

    try:
        userinfo = await _load_userinfo(token)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)

    client: httpx.AsyncClient = app.state.http_client
    upstream_url = httpx.URL(UPSTREAM_MCP_URL).copy_with(query=request.url.query.encode())
    body = await request.body()
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=_upstream_headers(request, userinfo),
        content=body,
    )
    upstream_response = await client.send(upstream_request, stream=True)
    response_headers = {
        name: value
        for name, value in upstream_response.headers.items()
        if name.lower() not in STRIPPED_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _stream_response(upstream_response),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_response.aclose),
    )


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def startup() -> None:
    app.state.http_client = httpx.AsyncClient(timeout=None)


async def shutdown() -> None:
    await app.state.http_client.aclose()


app = Starlette(
    debug=False,
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/{path:path}", proxy_mcp, methods=["GET", "POST", "DELETE", "PUT", "PATCH"]),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)


def main() -> None:
    """Run the local auth proxy with uvicorn."""
    import uvicorn

    host = os.getenv("LOCAL_AUTH_PROXY_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("LOCAL_AUTH_PROXY_BIND_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)
