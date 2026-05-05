"""Cognito HTTP callback URL resolution."""

import sys

import pytest

from plane_mcp.cognito_http import (
    REQUIRED_HTTP_ENV_VARS,
    _allowed_client_redirect_uris,
    _cognito_callback_base_and_path,
    cognito_http_configuration_intended,
    cognito_http_env_missing,
    cognito_http_env_ready,
    cognito_idp_redirect_uri,
    validate_cognito_http_env,
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


def test_cognito_http_configuration_intended(monkeypatch):
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    assert cognito_http_configuration_intended() is False
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    assert cognito_http_configuration_intended() is True
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
    assert cognito_http_configuration_intended() is True


def test_cognito_http_env_missing_lists_blank_vars(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.setenv(name, "x")
    assert cognito_http_env_missing() == []
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "")
    assert "MCP_JWT_SIGNING_KEY" in cognito_http_env_missing()


def test_validate_cognito_http_env_succeeds_when_required_set(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.setenv(name, "x")
    validate_cognito_http_env()


def test_http_main_raises_clear_error_when_cognito_partial(monkeypatch):
    monkeypatch.delenv("MCP_BASE_URL", raising=False)
    monkeypatch.delenv("COGNITO_AWS_REGION", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "test-pool")
    monkeypatch.setattr(sys, "argv", ["plane_mcp", "http"])
    import plane_mcp.__main__ as entry

    with pytest.raises(ValueError, match="Cognito is partially configured"):
        entry.main()


def test_cognito_idp_redirect_legacy_alias(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://x.test")
    monkeypatch.delenv("MCP_COGNITO_REDIRECT_URI", raising=False)
    monkeypatch.setenv("PLANE_MCP_COGNITO_REDIRECT_URI", "https://other.example/auth/callback")
    b, p = _cognito_callback_base_and_path()
    assert b == "https://other.example"
    assert p == "/auth/callback"
