"""Unit tests for the ``list_workspaces`` tool helpers."""

from types import SimpleNamespace
from unittest.mock import patch

from plane_mcp.tools.workspaces import _httpx_verify, _plane_auth_headers, _plane_root_base


def test_plane_root_base_uses_internal_first(monkeypatch):
    monkeypatch.setenv("PLANE_INTERNAL_BASE_URL", "http://plane-api:8000")
    monkeypatch.setenv("PLANE_BASE_URL", "https://public.example/")
    assert _plane_root_base() == "http://plane-api:8000"


def test_plane_root_base_falls_back_to_public(monkeypatch):
    monkeypatch.delenv("PLANE_INTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("PLANE_BASE_URL", "https://foss-pm.local.moneta.dev/")
    assert _plane_root_base() == "https://foss-pm.local.moneta.dev"


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


def test_httpx_verify_prefers_requests_ca_bundle_over_ssl_cert_file(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/ssl.pem")
    assert _httpx_verify() == "/ca.pem"


def test_httpx_verify_prefers_requests_ca_bundle(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/path/to/ca.pem")
    assert _httpx_verify() == "/path/to/ca.pem"


def test_httpx_verify_falls_back_to_ssl_cert_file(monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/path/to/cert.pem")
    assert _httpx_verify() == "/path/to/cert.pem"


def test_httpx_verify_defaults_true_when_no_bundle(monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert _httpx_verify() is True
