"""Moneta fork — Cognito HTTP path: env validation, id_token extraction, bearer forwarding."""

import asyncio

import jwt
import pytest

from plane_mcp.moneta.client import bearer_for, plane_request_auth
from plane_mcp.moneta.cognito import (
    COGNITO_USERNAME_CLAIM,
    EMAIL_CLAIM,
    ID_TOKEN_KEY,
    PlaneCognitoProvider,
)
from plane_mcp.moneta.http import validate_env

_COGNITO_ENV = {
    "MCP_BASE_URL": "https://pm-mcp.example.test",
    "COGNITO_USER_POOL_ID": "us-east-1_abc",
    "COGNITO_AWS_REGION": "us-east-1",
    "OIDC_CLIENT_ID": "client-123",
    "MCP_JWT_SIGNING_KEY": "deadbeef",
}

_COGNITO_KEYS = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "MCP_JWT_SIGNING_KEY",
    "MCP_ENV",
    "MCP_OAUTH_STORAGE_URL",
)


def _set_env(monkeypatch, env):
    for key in _COGNITO_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_validate_env_ok(monkeypatch):
    _set_env(monkeypatch, _COGNITO_ENV)
    validate_env()  # must not raise


def test_validate_env_missing_pool(monkeypatch):
    env = {k: v for k, v in _COGNITO_ENV.items() if k != "COGNITO_USER_POOL_ID"}
    _set_env(monkeypatch, env)
    with pytest.raises(ValueError, match="COGNITO_USER_POOL_ID"):
        validate_env()


def test_validate_env_requires_signing_material(monkeypatch):
    env = {k: v for k, v in _COGNITO_ENV.items() if k != "MCP_JWT_SIGNING_KEY"}
    _set_env(monkeypatch, env)
    with pytest.raises(ValueError, match="MCP_JWT_SIGNING_KEY"):
        validate_env()


def test_extract_upstream_claims_stashes_id_token():
    # __new__ avoids the provider's networked __init__ (OIDC discovery); the
    # override only reads its argument, module constants, and PyJWT.
    provider = PlaneCognitoProvider.__new__(PlaneCognitoProvider)
    id_token = jwt.encode(
        {"email": "u@x.com", "cognito:username": "1020", "sub": "uuid"},
        "k",
        algorithm="HS256",
    )
    result = asyncio.run(provider._extract_upstream_claims({"id_token": id_token, "access_token": "AT"}))
    assert result == {
        ID_TOKEN_KEY: id_token,
        EMAIL_CLAIM: "u@x.com",
        COGNITO_USERNAME_CLAIM: "1020",
    }


def test_extract_upstream_claims_no_id_token_returns_none():
    provider = PlaneCognitoProvider.__new__(PlaneCognitoProvider)
    assert asyncio.run(provider._extract_upstream_claims({"access_token": "AT"})) is None


def test_bearer_prefers_id_token():
    assert bearer_for("ref", {"upstream_claims": {"id_token": "IDT"}}) == "IDT"


def test_bearer_falls_back_to_token():
    assert bearer_for("ref", {}) == "ref"
    assert bearer_for("ref", {"upstream_claims": {}}) == "ref"
    assert bearer_for("ref", None) == "ref"


def _install_fake_oauth_internals(monkeypatch, raw_token_data):
    """Patch the OAuthProxy internals load_access_token's JTI lookup walks.

    Patched at the class level: ``jwt_issuer`` is a read-only property and the
    stores are pydantic-managed instance attrs, neither settable on a bare
    ``__new__`` instance.
    """

    class _Issuer:
        def verify_token(self, token):
            return {"jti": "J1"}

    class _MappingStore:
        async def get(self, key):
            return type("M", (), {"upstream_token_id": "U1"})()

    class _TokenStore:
        async def get(self, key):
            return type("T", (), {"raw_token_data": raw_token_data})()

    monkeypatch.setattr(PlaneCognitoProvider, "jwt_issuer", _Issuer(), raising=False)
    monkeypatch.setattr(PlaneCognitoProvider, "_jti_mapping_store", _MappingStore(), raising=False)
    monkeypatch.setattr(PlaneCognitoProvider, "_upstream_token_store", _TokenStore(), raising=False)


def test_resolve_upstream_claims_picks_id_token_identity(monkeypatch):
    # Reproduces the federated-user case: access-token sub is an opaque UUID
    # (09daf50c…) but the id_token carries the human email/username (1020…).
    # _resolve_upstream_claims must surface the id_token identity, not the UUID.
    id_token = jwt.encode(
        {"email": "1020010000020127@askii.ai", "cognito:username": "1020010000020127", "sub": "09daf50c"},
        "k",
        algorithm="HS256",
    )
    _install_fake_oauth_internals(monkeypatch, {"id_token": id_token, "access_token": "AT"})
    provider = PlaneCognitoProvider.__new__(PlaneCognitoProvider)

    result = asyncio.run(provider._resolve_upstream_claims("ref-jwt"))

    assert result == {
        ID_TOKEN_KEY: id_token,
        EMAIL_CLAIM: "1020010000020127@askii.ai",
        COGNITO_USERNAME_CLAIM: "1020010000020127",
    }


def test_resolve_upstream_claims_returns_none_without_id_token(monkeypatch):
    _install_fake_oauth_internals(monkeypatch, {"access_token": "AT"})  # no id_token
    provider = PlaneCognitoProvider.__new__(PlaneCognitoProvider)

    assert asyncio.run(provider._resolve_upstream_claims("ref-jwt")) is None


def test_plane_request_auth_stdio_uses_env(monkeypatch):
    # No HTTP request scope → get_access_token() raises → env credentials apply.
    monkeypatch.setenv("PLANE_API_KEY", "pat-xyz")
    monkeypatch.delenv("PLANE_INTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("PLANE_BASE_URL", "https://plane.example")

    base_url, headers = plane_request_auth()

    assert base_url == "https://plane.example"
    assert headers["X-Api-Key"] == "pat-xyz"
    assert "Authorization" not in headers
