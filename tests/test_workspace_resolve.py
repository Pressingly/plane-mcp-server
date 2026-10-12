"""Moneta fork — workspace resolution precedence (arg > claim > env > error)."""

import pytest

import plane_mcp.moneta.workspace as ws
from plane_mcp.moneta.workspace import WorkspaceError, resolve_workspace


def test_arg_override_wins():
    token = ws.set_override("acme")
    try:
        assert resolve_workspace("claimws", "envws") == "acme"
    finally:
        ws.reset_override(token)


def test_claim_when_no_arg():
    assert resolve_workspace("claimws", "envws") == "claimws"


def test_env_when_no_arg_no_claim():
    assert resolve_workspace(None, "envws") == "envws"


def test_raises_when_nothing_resolves():
    with pytest.raises(WorkspaceError, match="workspace_slug"):
        resolve_workspace(None, "")


def test_override_reset_clears_arg():
    token = ws.set_override("acme")
    ws.reset_override(token)
    assert resolve_workspace("claimws", "envws") == "claimws"


def test_blank_candidates_are_ignored():
    token = ws.set_override("   ")  # whitespace-only → ignored
    try:
        assert resolve_workspace("  ", "envws") == "envws"
    finally:
        ws.reset_override(token)


COGNITO_CLAIMS = {"upstream_claims": {"email": "u@example.com"}}


def test_cognito_token_falls_back_to_the_environment():
    assert ws.workspace_for(COGNITO_CLAIMS, "envws") == "envws"


def test_stdio_falls_back_to_the_environment():
    assert ws.workspace_for(None, "envws") == "envws"


def test_cognito_token_without_any_workspace_raises_the_guiding_error():
    with pytest.raises(WorkspaceError, match="list_workspaces"):
        ws.workspace_for(COGNITO_CLAIMS, "")


def test_plane_token_is_bound_to_its_claim_even_when_empty():
    assert ws.workspace_for({"auth_method": "oauth"}, "envws") == ""
    assert ws.workspace_for({"workspace_slug": "claimws"}, "envws") == "claimws"


def test_arg_override_wins_over_a_plane_token_claim():
    token = ws.set_override("acme")
    try:
        assert ws.workspace_for({"workspace_slug": "claimws"}, "envws") == "acme"
    finally:
        ws.reset_override(token)
