"""Tests for Plane client context resolution."""

import pytest
from plane.errors import ConfigurationError

import plane_mcp.client as client_mod


def test_get_plane_client_context_raises_when_workspace_slug_unresolved(monkeypatch):
    """OAuth/browser sessions often have no claim; empty slug must not reach the SDK."""
    monkeypatch.setenv("PLANE_API_KEY", "test-api-key")
    monkeypatch.delenv("PLANE_WORKSPACE_SLUG", raising=False)
    monkeypatch.setattr(client_mod, "get_access_token", lambda: None)

    with pytest.raises(ConfigurationError, match="Workspace slug is required"):
        client_mod.get_plane_client_context()


def test_get_plane_client_context_uses_env_slug(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "test-api-key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "my-ws")
    monkeypatch.setattr(client_mod, "get_access_token", lambda: None)

    ctx = client_mod.get_plane_client_context()
    assert ctx.workspace_slug == "my-ws"


def test_get_plane_client_context_tool_arg_wins(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "test-api-key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "env-ws")
    monkeypatch.setattr(client_mod, "get_access_token", lambda: None)

    ctx = client_mod.get_plane_client_context(workspace_slug_from_client="tool-ws")
    assert ctx.workspace_slug == "tool-ws"
