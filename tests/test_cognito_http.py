"""Cognito HTTP callback URL resolution."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy

from plane_mcp.cognito_http import (
    REQUIRED_HTTP_ENV_VARS,
    _allowed_client_redirect_uris,
    _cognito_callback_base_and_path,
    _PlaneCognitoProvider,
    cognito_http_env_missing,
    validate_cognito_http_env,
)


@pytest.fixture
def clear_cognito_env(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_cognito_callback_uses_mcp_base_url(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example/")
    b, p = _cognito_callback_base_and_path()
    assert b == "https://mcp.example"
    assert p == "/auth/callback"


def test_allowed_client_redirect_uris_none_when_unset(clear_cognito_env, monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
    assert _allowed_client_redirect_uris() is None


def test_validate_cognito_http_env_fails_when_redirect_patterns_only_commas(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", " , , ")
    with pytest.raises(ValueError, match="MCP_ALLOWED_CLIENT_REDIRECT_URIS"):
        validate_cognito_http_env()


def test_allowed_client_redirect_uris_when_set(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "cursor://*,http://localhost:*/*")
    assert _allowed_client_redirect_uris() == ["cursor://*", "http://localhost:*/*"]


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




def _access_token() -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=[], expires_at=None, claims={})


def _provider_load_token_setup(monkeypatch, *, jti_payload: dict, jti_mapping, upstream_store_val):
    validated = _access_token()

    async def fake_super_load(self, token: str):
        return validated

    monkeypatch.setattr(OIDCProxy, "load_access_token", fake_super_load)

    provider = object.__new__(_PlaneCognitoProvider)
    mock_issuer = MagicMock()
    mock_issuer.verify_token = MagicMock(return_value=jti_payload)
    object.__setattr__(provider, "_jwt_issuer", mock_issuer)
    jti_store = AsyncMock()
    jti_store.get = AsyncMock(return_value=jti_mapping)
    object.__setattr__(provider, "_jti_mapping_store", jti_store)
    up_store = AsyncMock()
    up_store.get = AsyncMock(return_value=upstream_store_val)
    object.__setattr__(provider, "_upstream_token_store", up_store)
    return provider


def test_plane_cognito_load_access_token_returns_none_without_jti(monkeypatch):
    p = _provider_load_token_setup(monkeypatch, jti_payload={}, jti_mapping=None, upstream_store_val=None)
    assert asyncio.run(_PlaneCognitoProvider.load_access_token(p, "raw")) is None


def test_plane_cognito_load_access_token_returns_none_without_id_token(monkeypatch):
    p = _provider_load_token_setup(
        monkeypatch,
        jti_payload={"jti": "j1"},
        jti_mapping={"upstream_token_id": "up1"},
        upstream_store_val={"raw_token_data": {}},
    )
    assert asyncio.run(_PlaneCognitoProvider.load_access_token(p, "raw")) is None


def test_plane_cognito_load_access_token_attaches_id_token(monkeypatch):
    p = _provider_load_token_setup(
        monkeypatch,
        jti_payload={"jti": "j1"},
        jti_mapping={"upstream_token_id": "up1"},
        upstream_store_val={"raw_token_data": {"id_token": "id.jwt"}},
    )
    out = asyncio.run(_PlaneCognitoProvider.load_access_token(p, "raw"))
    assert out is not None
    assert out.claims.get("id_token") == "id.jwt"
