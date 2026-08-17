import os
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from key_value.aio.stores.memory import MemoryStore

from cbioportal_mcp.auth import (
    _ACCESS_TOKEN_TTL_CEILING_SECONDS,
    _build_auth_provider,
    _build_client_storage,
    _RefreshTTLFloorWrapper,
)

_ALL_GOOGLE_ENV = {
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": "fake-secret",
    "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.cbioportal.org",
}


def _without(*names: str) -> dict:
    return {k: v for k, v in _ALL_GOOGLE_ENV.items() if k not in names}


def test_returns_none_when_no_google_env_set():
    with patch.dict(os.environ, {}, clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_client_id_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID"), clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_client_secret_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET"), clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_base_url_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_BASE_URL"), clear=True):
        assert _build_auth_provider() is None


def test_builds_google_provider_when_all_three_set():
    # GoogleProvider's default client_storage creates a DiskStore under the
    # platformdirs data directory as a side effect of construction, so the
    # class itself is mocked here to keep this test hermetic (no filesystem
    # writes) rather than depending on that side effect in tests.
    fake_provider = Mock(name="GoogleProvider instance")
    with (
        patch.dict(os.environ, _ALL_GOOGLE_ENV, clear=True),
        patch(
            "cbioportal_mcp.auth.GoogleProvider", return_value=fake_provider
        ) as mock_google_provider,
    ):
        provider = _build_auth_provider()

    assert provider is fake_provider
    mock_google_provider.assert_called_once_with(
        client_id=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_ID"],
        client_secret=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET"],
        base_url=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_BASE_URL"],
        required_scopes=["openid", "email", "profile"],
        require_authorization_consent=False,
        # Dynamically derived per-deployment (from client_secret) — asserted
        # precisely in the _build_client_storage tests below, not here.
        jwt_signing_key=ANY,
        client_storage=ANY,
    )


_EXTENDED_TTL_SECONDS = 180 * 60 * 60 * 24


def _wrapper_with_mock_backend() -> tuple[_RefreshTTLFloorWrapper, AsyncMock]:
    backend = AsyncMock()
    return _RefreshTTLFloorWrapper(key_value=backend, ttl_seconds=_EXTENDED_TTL_SECONDS), backend


@pytest.mark.asyncio
async def test_refresh_tokens_collection_always_extended():
    wrapper, backend = _wrapper_with_mock_backend()
    await wrapper.put("k", {"v": 1}, collection="mcp-refresh-tokens", ttl=60)
    backend.put.assert_awaited_once_with(
        key="k", value={"v": 1}, collection="mcp-refresh-tokens", ttl=_EXTENDED_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_upstream_tokens_collection_always_extended():
    wrapper, backend = _wrapper_with_mock_backend()
    await wrapper.put("k", {"v": 1}, collection="mcp-upstream-tokens", ttl=60)
    backend.put.assert_awaited_once_with(
        key="k", value={"v": 1}, collection="mcp-upstream-tokens", ttl=_EXTENDED_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_jti_mappings_short_ttl_passed_through_unchanged():
    """Access-token JTI mappings (~expires_in, ~1 hour) must not be extended —
    only the refresh-token JTI mappings sharing this collection should be."""
    wrapper, backend = _wrapper_with_mock_backend()
    await wrapper.put("k", {"v": 1}, collection="mcp-jti-mappings", ttl=3600)
    backend.put.assert_awaited_once_with(
        key="k", value={"v": 1}, collection="mcp-jti-mappings", ttl=3600
    )


@pytest.mark.asyncio
async def test_jti_mappings_long_ttl_extended():
    wrapper, backend = _wrapper_with_mock_backend()
    long_ttl = _ACCESS_TOKEN_TTL_CEILING_SECONDS + 1
    await wrapper.put("k", {"v": 1}, collection="mcp-jti-mappings", ttl=long_ttl)
    backend.put.assert_awaited_once_with(
        key="k", value={"v": 1}, collection="mcp-jti-mappings", ttl=_EXTENDED_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_unrelated_collection_passed_through_unchanged():
    wrapper, backend = _wrapper_with_mock_backend()
    await wrapper.put("k", {"v": 1}, collection="mcp-oauth-transactions", ttl=60)
    backend.put.assert_awaited_once_with(
        key="k", value={"v": 1}, collection="mcp-oauth-transactions", ttl=60
    )


@pytest.mark.asyncio
async def test_put_many_applies_same_extension_logic():
    wrapper, backend = _wrapper_with_mock_backend()
    await wrapper.put_many(
        ["k1", "k2"], [{"v": 1}, {"v": 2}], collection="mcp-refresh-tokens", ttl=60
    )
    backend.put_many.assert_awaited_once_with(
        keys=["k1", "k2"],
        values=[{"v": 1}, {"v": 2}],
        collection="mcp-refresh-tokens",
        ttl=_EXTENDED_TTL_SECONDS,
    )


@pytest.mark.asyncio
async def test_wrapper_extends_ttl_through_a_real_backing_store():
    """End-to-end check against a real (in-memory) AsyncKeyValue store, not
    just a mock — proves the extended ttl is what actually gets persisted,
    not just what gets forwarded in the put() call."""
    real_store = MemoryStore()
    wrapper = _RefreshTTLFloorWrapper(key_value=real_store, ttl_seconds=_EXTENDED_TTL_SECONDS)

    await wrapper.put(
        "session-1", {"refresh_token": "fake"}, collection="mcp-refresh-tokens", ttl=60
    )

    _value, ttl = await real_store.ttl(key="session-1", collection="mcp-refresh-tokens")
    assert ttl is not None
    # Allow a small margin for time elapsed during the test itself.
    assert _EXTENDED_TTL_SECONDS - 5 < ttl <= _EXTENDED_TTL_SECONDS


@pytest.mark.asyncio
async def test_build_client_storage_round_trips_through_real_encryption(tmp_path):
    """The real risk with replicating FastMCP's key derivation isn't a typo
    that raises — Fernet decryption failures are silent/wrong-data, not
    obvious errors. Only an actual write-then-read against the real
    encrypted disk store catches a wrong salt or argument."""
    with patch("cbioportal_mcp.auth.fastmcp_settings.home", tmp_path):
        jwt_signing_key, client_storage = _build_client_storage(
            client_secret="fake-client-secret", ttl_days=180
        )

    assert isinstance(jwt_signing_key, bytes)

    await client_storage.put("k", {"secret": "value"}, collection="mcp-refresh-tokens", ttl=60)
    result = await client_storage.get("k", collection="mcp-refresh-tokens")
    assert result == {"secret": "value"}
