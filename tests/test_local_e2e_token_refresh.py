"""The local e2e harness never sends an expired Keycloak token.

scripts/local_e2e_keycloak_authz.py keeps the realm's short (300s) token
lifetime and re-mints a user's token once less than
TOKEN_REFRESH_MARGIN_SECONDS remain. Fake clock, no Keycloak.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "local_e2e_keycloak_authz.py"
_spec = importlib.util.spec_from_file_location("local_e2e_keycloak_authz", SCRIPT)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _token(clock, expires_in=300):
    token = e2e.KeycloakToken("http://kc", "alice", "pw", clock=clock)
    minted = []

    async def mint():
        await asyncio.sleep(0)  # yield, so concurrent callers really overlap
        minted.append(clock())
        return f"jwt-{len(minted)}", expires_in

    token._mint = mint
    return token, minted


def test_token_is_reused_until_the_refresh_margin_then_reminted():
    clock = Clock()
    token, minted = _token(clock)

    async def run():
        assert await token.value() == "jwt-1"
        clock.now += 300 - e2e.TOKEN_REFRESH_MARGIN_SECONDS - 1  # 61s of life left
        assert await token.value() == "jwt-1"
        clock.now += 1  # 60s left: re-mint
        assert await token.value() == "jwt-2"
        clock.now += 10_000  # long past expiry
        assert await token.value() == "jwt-3"

    asyncio.run(run())
    assert len(minted) == 3


def test_concurrent_callers_mint_once():
    clock = Clock()
    token, minted = _token(clock)

    async def run():
        return await asyncio.gather(*[token.value() for _ in range(12)])

    assert asyncio.run(run()) == ["jwt-1"] * 12
    assert len(minted) == 1


def test_short_lived_token_is_reminted_every_call():
    # expires_in below the margin: never reuse it.
    clock = Clock()
    token, minted = _token(clock, expires_in=30)

    async def run():
        assert await token.value() == "jwt-1"
        assert await token.value() == "jwt-2"

    asyncio.run(run())


def test_call_with_token_sends_the_fresh_value(monkeypatch):
    clock = Clock()
    token, _minted = _token(clock)
    sent = []

    class FakeTransport:
        def __init__(self, url, headers):
            sent.append(headers["authorization"])

    class FakeClient:
        def __init__(self, transport):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, *args, **kwargs):
            return None

    monkeypatch.setattr(e2e, "StreamableHttpTransport", FakeTransport)
    monkeypatch.setattr(e2e, "Client", FakeClient)
    monkeypatch.setattr(e2e, "_payload", lambda result: result)

    async def run():
        await e2e._call_with_token("http://proxy/mcp/", token, "tool")
        clock.now += 400
        await e2e._call_with_token("http://proxy/mcp/", token, "tool")
        await e2e._call_with_token("http://proxy/mcp/", "not-a-token", "tool")

    asyncio.run(run())
    assert sent == ["Bearer jwt-1", "Bearer jwt-2", "Bearer not-a-token"]
    assert "jwt" not in repr(token)
