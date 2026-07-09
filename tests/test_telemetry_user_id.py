from unittest.mock import patch

from cbioportal_mcp.telemetry import _extract_user_id


def test_extracts_user_id_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "507f1f77bcf86cd799439011"}):
        assert _extract_user_id() == "507f1f77bcf86cd799439011"


def test_returns_none_when_header_absent():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        assert _extract_user_id() is None


def test_returns_none_when_empty_string():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": ""}):
        assert _extract_user_id() is None
