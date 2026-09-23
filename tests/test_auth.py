import os
from unittest.mock import Mock, patch

from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from cbioportal_mcp.auth import (
    _build_auth_provider,
    _build_client_storage,
    _derive_storage_encryption_key,
)

_ALL_GOOGLE_ENV = {
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": "fake-secret",
    "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.cbioportal.org",
}

_ALL_GOOGLE_AND_REDIS_ENV = {
    **_ALL_GOOGLE_ENV,
    "REDIS_URL": "redis://localhost:6379/9",
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
    )


def test_builds_google_provider_without_client_storage_when_redis_url_unset():
    # No REDIS_URL: falls back to GoogleProvider's own default (disk) store,
    # so no client_storage kwarg should be passed at all.
    fake_provider = Mock(name="GoogleProvider instance")
    with (
        patch.dict(os.environ, _ALL_GOOGLE_ENV, clear=True),
        patch(
            "cbioportal_mcp.auth.GoogleProvider", return_value=fake_provider
        ) as mock_google_provider,
    ):
        provider = _build_auth_provider()

    assert provider is fake_provider
    _, kwargs = mock_google_provider.call_args
    assert "client_storage" not in kwargs


def test_builds_google_provider_with_redis_client_storage_when_redis_url_set():
    fake_provider = Mock(name="GoogleProvider instance")
    with (
        patch.dict(os.environ, _ALL_GOOGLE_AND_REDIS_ENV, clear=True),
        patch(
            "cbioportal_mcp.auth.GoogleProvider", return_value=fake_provider
        ) as mock_google_provider,
    ):
        provider = _build_auth_provider()

    assert provider is fake_provider
    _, kwargs = mock_google_provider.call_args
    assert isinstance(kwargs["client_storage"], FernetEncryptionWrapper)
    assert isinstance(kwargs["client_storage"].key_value, RedisStore)


def test_derive_storage_encryption_key_is_deterministic():
    assert _derive_storage_encryption_key("fake-secret") == _derive_storage_encryption_key(
        "fake-secret"
    )


def test_derive_storage_encryption_key_differs_per_secret():
    assert _derive_storage_encryption_key("fake-secret") != _derive_storage_encryption_key(
        "other-secret"
    )


def test_build_client_storage_wraps_redis_store_with_fernet_encryption():
    storage = _build_client_storage("fake-secret", "redis://localhost:6379/9")

    assert isinstance(storage, FernetEncryptionWrapper)
    assert isinstance(storage.key_value, RedisStore)
