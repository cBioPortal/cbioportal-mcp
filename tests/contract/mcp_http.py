"""A deliberately minimal streamable-HTTP MCP client for the contract tests.

Not the fastmcp/mcp SDK client on purpose: those follow redirects and hide
status codes, and the failure we most need to catch is exactly that kind of
transport-level behaviour (the FastMCP trailing-slash 307 -> 406 -> 404 loop
that broke every chat tool call). Every HTTP exchange is recorded, redirects
are never followed, and any non-2xx response fails loudly.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field

import httpx

PROTOCOL_VERSION = "2025-03-26"

# What chat.cbioportal.org's LibreChat sends on every MCP request.
LIBRECHAT_HEADERS = {
    "x-user-id": "contract-test-user",
    "x-user-email": "contract-test@example.org",
    "x-user-email-verified": "true",
    "x-cbioportal-mcp-bridge-secret": "contract-test-not-a-secret",
}


class McpHttpError(AssertionError):
    pass


@dataclass
class Exchange:
    method: str
    url: str
    status: int
    location: str | None


@dataclass
class McpSession:
    url: str
    headers: dict = field(default_factory=lambda: dict(LIBRECHAT_HEADERS))
    timeout: float = 60.0
    session_id: str | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self):
        self._client = httpx.Client(follow_redirects=False, timeout=self.timeout)

    def close(self):
        try:
            if self.session_id:
                self._client.delete(self.url, headers=self._headers())
        finally:
            self._client.close()

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict:
        h = {
            **self.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def post(self, payload: dict) -> httpx.Response:
        resp = self._client.post(self.url, headers=self._headers(), json=payload)
        self.exchanges.append(
            Exchange("POST", self.url, resp.status_code, resp.headers.get("location"))
        )
        if not 200 <= resp.status_code < 300:
            raise McpHttpError(
                f"POST {self.url} -> HTTP {resp.status_code}"
                + (f" (Location: {resp.headers['location']})" if "location" in resp.headers else "")
                + f"\n{resp.text[:500]}"
            )
        return resp

    def request(self, method: str, params: dict | None = None) -> dict:
        req_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = self.post(payload)
        if sid := resp.headers.get("mcp-session-id"):
            self.session_id = sid
        message = _response_message(resp, req_id)
        if "error" in message:
            raise McpHttpError(f"{method} returned JSON-RPC error: {message['error']}")
        return message["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.post(payload)

    # -- MCP ----------------------------------------------------------------

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cbioportal-mcp-contract-tests", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_all(self, method: str, key: str) -> list[dict]:
        items, cursor = [], None
        while True:
            result = self.request(method, {"cursor": cursor} if cursor else {})
            items += result[key]
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})


def _response_message(resp: httpx.Response, req_id: int) -> dict:
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return resp.json()
    if ctype.startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if msg.get("id") == req_id:
                    return msg
        raise McpHttpError(f"no response with id {req_id} in event stream:\n{resp.text[:500]}")
    raise McpHttpError(f"unexpected content-type {ctype!r}: {resp.text[:500]}")


def tool_payload(result: dict):
    """The tool's return value: structuredContent's `result` wrapper unwrapped
    (FastMCP wraps non-object returns), else the text content parsed as JSON
    when possible."""
    structured = result.get("structuredContent")
    if structured is not None:
        if set(structured) == {"result"}:
            return structured["result"]
        return structured
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    text = "\n".join(texts)
    try:
        return json.loads(text)
    except ValueError:
        return text
