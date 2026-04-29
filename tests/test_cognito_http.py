"""Cognito HTTP callback URL resolution."""

import pytest

from plane_mcp.cognito_http import (
    REQUIRED_HTTP_ENV_VARS,
    _allowed_client_redirect_uris,
    _cognito_callback_base_and_path,
    cognito_http_env_ready,
    cognito_idp_redirect_uri,
)


@pytest.fixture
def clear_cognito_env(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_cognito_http_env_ready(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.setenv(name, "x")
    assert cognito_http_env_ready() is True


def test_cognito_http_env_ready_false(clear_cognito_env, monkeypatch):
    for i, _name in enumerate(REQUIRED_HTTP_ENV_VARS):
        for n in REQUIRED_HTTP_ENV_VARS:
            monkeypatch.delenv(n, raising=False)
        for j, n in enumerate(REQUIRED_HTTP_ENV_VARS):
            if j != i:
                monkeypatch.setenv(n, "x")
        assert cognito_http_env_ready() is False


def test_cognito_idp_redirect_from_mcp_base(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example/")
    assert cognito_idp_redirect_uri() == "https://mcp.example/auth/callback"


def test_cognito_idp_redirect_explicit(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://internal:8211")
    monkeypatch.setenv("MCP_COGNITO_REDIRECT_URI", "https://public.example/cb/path")
    assert cognito_idp_redirect_uri() == "https://public.example/cb/path"


def test_cognito_idp_redirect_rejects_query(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://x.test")
    monkeypatch.setenv("MCP_COGNITO_REDIRECT_URI", "https://x.test/cb?a=1")
    with pytest.raises(ValueError, match="query"):
        cognito_idp_redirect_uri()


def test_allowed_client_redirect_uris_none_when_unset(clear_cognito_env, monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
    assert _allowed_client_redirect_uris() is None


def test_allowed_client_redirect_uris_when_set(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "cursor://*,http://localhost:*/*")
    assert _allowed_client_redirect_uris() == ["cursor://*", "http://localhost:*/*"]


def test_cognito_idp_redirect_legacy_alias(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://x.test")
    monkeypatch.delenv("MCP_COGNITO_REDIRECT_URI", raising=False)
    monkeypatch.setenv("PLANE_MCP_COGNITO_REDIRECT_URI", "https://other.example/auth/callback")
    b, p = _cognito_callback_base_and_path()
    assert b == "https://other.example"
    assert p == "/auth/callback"
