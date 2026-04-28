"""Unit tests for Cognito HTTP env gating and client workspace slug."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plane_mcp.client import get_plane_client_context
from plane_mcp.cognito_http import REQUIRED_HTTP_ENV_VARS, cognito_http_env_ready


@pytest.fixture
def clear_cognito_env(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_cognito_http_env_ready_false_when_any_missing(clear_cognito_env, monkeypatch):
    for i, name in enumerate(REQUIRED_HTTP_ENV_VARS):
        for n in REQUIRED_HTTP_ENV_VARS:
            monkeypatch.delenv(n, raising=False)
        for j, n in enumerate(REQUIRED_HTTP_ENV_VARS):
            if j != i:
                monkeypatch.setenv(n, "x")
        assert cognito_http_env_ready() is False


def test_cognito_http_env_ready_true(monkeypatch):
    for name in REQUIRED_HTTP_ENV_VARS:
        monkeypatch.setenv(name, "set")
    assert cognito_http_env_ready() is True


@patch("plane_mcp.client.get_access_token")
def test_workspace_slug_from_env_when_claim_empty(mock_get_token, monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "from-env")
    monkeypatch.setenv("PLANE_BASE_URL", "https://api.example.test")
    mock_get_token.return_value = SimpleNamespace(
        token="token",
        claims={"auth_method": "oauth"},
    )
    ctx = get_plane_client_context()
    assert ctx.workspace_slug == "from-env"


@patch("plane_mcp.client.get_access_token")
def test_workspace_slug_from_claim_when_present(mock_get_token, monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "from-env")
    monkeypatch.setenv("PLANE_BASE_URL", "https://api.example.test")
    mock_get_token.return_value = SimpleNamespace(
        token="token",
        claims={"auth_method": "oauth", "workspace_slug": "from-claim"},
    )
    ctx = get_plane_client_context()
    assert ctx.workspace_slug == "from-claim"
