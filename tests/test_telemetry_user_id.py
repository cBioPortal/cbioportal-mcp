import base64
from unittest.mock import patch

from cbioportal_mcp.telemetry import _extract_user_identity


def test_extracts_user_id_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "507f1f77bcf86cd799439011"}):
        user_id, user_email = _extract_user_identity()
        assert user_id == "507f1f77bcf86cd799439011"
        assert user_email is None


def test_extracts_user_email_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "abc123", "x-user-email": "user@example.com"}):
        user_id, user_email = _extract_user_identity()
        assert user_id == "abc123"
        assert user_email == "user@example.com"


def test_decodes_base64_email():
    encoded = "b64:" + base64.b64encode(b"user+tag@example.com").decode()
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-email": encoded}):
        _, user_email = _extract_user_identity()
        assert user_email == "user+tag@example.com"


def test_returns_none_when_headers_absent():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        user_id, user_email = _extract_user_identity()
        assert user_id is None
        assert user_email is None


def test_returns_none_when_empty_string():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "", "x-user-email": ""}):
        user_id, user_email = _extract_user_identity()
        assert user_id is None
        assert user_email is None
