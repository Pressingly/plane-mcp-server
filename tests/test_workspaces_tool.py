"""Unit tests for the ``list_workspaces`` tool helpers."""

from types import SimpleNamespace
from unittest.mock import patch

from plane_mcp.tools.workspaces import _plane_auth_headers


@patch("plane_mcp.tools.workspaces.get_access_token")
def test_plane_auth_headers_oauth_uses_bearer(mock_get_token, monkeypatch):
    monkeypatch.delenv("PLANE_API_KEY", raising=False)
    mock_get_token.return_value = SimpleNamespace(token="cognito-jwt", claims={"auth_method": "oauth"})
    headers = _plane_auth_headers()
    assert headers["Authorization"] == "Bearer cognito-jwt"
    assert "X-Api-Key" not in headers


@patch("plane_mcp.tools.workspaces.get_access_token")
def test_plane_auth_headers_oauth_prefers_id_token_for_bearer(mock_get_token, monkeypatch):
    monkeypatch.delenv("PLANE_API_KEY", raising=False)
    mock_get_token.return_value = SimpleNamespace(
        token="cognito-access",
        claims={"auth_method": "oauth", "id_token": "cognito-id.jwt"},
    )
    headers = _plane_auth_headers()
    assert headers["Authorization"] == "Bearer cognito-id.jwt"


@patch("plane_mcp.tools.workspaces.get_access_token")
def test_plane_auth_headers_pat_uses_x_api_key(mock_get_token, monkeypatch):
    monkeypatch.delenv("PLANE_API_KEY", raising=False)
    mock_get_token.return_value = SimpleNamespace(token="pat-value", claims={"auth_method": "api_key_header"})
    headers = _plane_auth_headers()
    assert headers["X-Api-Key"] == "pat-value"
    assert "Authorization" not in headers


@patch("plane_mcp.tools.workspaces.get_access_token", return_value=None)
def test_plane_auth_headers_stdio_falls_back_to_env_api_key(_mock_get_token, monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "env-key")
    headers = _plane_auth_headers()
    assert headers["X-Api-Key"] == "env-key"
    assert "Authorization" not in headers
