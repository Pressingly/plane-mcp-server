"""Workspace selection for the Moneta fork (per-call, via an injected tool arg).

In Cognito http mode Claude can't send ``X-Workspace-Slug`` and the Cognito token
has no ``workspace_slug`` claim, so an optional ``workspace_slug`` argument is
**injected onto every workspace-scoped tool** by :mod:`plane_mcp.moneta.inject`
(no per-tool edits). That arg is stashed in a ContextVar for the duration of the
tool call; :func:`resolve_workspace` reads it here.

Precedence: per-call ``workspace_slug`` arg → token ``workspace_slug`` claim
(PAT ``X-Workspace-Slug`` / Plane-OAuth installation) → ``PLANE_WORKSPACE_SLUG``
env → guiding error. Because the arg is per-call (a fresh ContextVar scope per
request), concurrent Claude conversations targeting different workspaces stay
isolated — no shared/sticky state.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

import requests
from fastmcp.utilities.logging import get_logger

from plane_mcp.moneta.client import plane_request_auth

logger = get_logger(__name__)

# Per-request override carrying the tool call's workspace_slug arg (set by
# plane_mcp.moneta.inject around forward(), read by resolve_workspace).
_override: ContextVar[str | None] = ContextVar("plane_workspace_slug", default=None)


class WorkspaceError(RuntimeError):
    """No workspace could be resolved; the message guides the MCP client."""


def set_override(slug: str | None) -> Token:
    return _override.set(slug)


def reset_override(token: Token) -> None:
    _override.reset(token)


def _fetch_workspaces() -> list[dict[str, Any]]:
    """GET Plane's ``/api/users/me/workspaces/`` and normalize to ``[{id,name,slug}]``.

    ``requests`` honors ``REQUESTS_CA_BUNDLE`` for the devstack's self-signed cert.
    Used by the ``list_workspaces`` discovery tool.
    """
    base_url, headers = plane_request_auth()
    url = f"{base_url.rstrip('/')}/api/users/me/workspaces/"
    logger.debug("workspaces: GET %s", url)
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("workspaces") or []
    result = [
        {"id": ws.get("id"), "name": ws.get("name"), "slug": ws.get("slug")} for ws in payload if isinstance(ws, dict)
    ]
    logger.debug("workspaces: %d returned (status=%s)", len(result), response.status_code)
    return result


def resolve_workspace(claim_slug: str | None, env_default: str) -> str:
    """Resolve the target workspace: per-call arg → token claim → env → raise.

    The per-call arg comes from the ContextVar set by :mod:`plane_mcp.moneta.inject`.
    Raises :class:`WorkspaceError` (surfaced to the MCP client) when nothing resolves.
    """
    for source, candidate in (("arg", _override.get()), ("claim", claim_slug), ("env", env_default)):
        if candidate and candidate.strip():
            logger.debug("workspace resolve: source=%s slug=%s", source, candidate.strip())
            return candidate.strip()
    raise WorkspaceError(
        "No Plane workspace specified. Pass workspace_slug to the tool (call "
        "list_workspaces to find your slug), or set PLANE_WORKSPACE_SLUG."
    )
