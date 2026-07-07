"""Extra MCP tools added by the Moneta fork.

Registered onto every transport via a one-line hook in
``plane_mcp.tools.register_tools`` so the upstream per-domain tool modules stay
untouched. Workspace fetch/resolution lives in :mod:`plane_mcp.moneta.workspace`;
the per-call ``workspace_slug`` arg is injected by :mod:`plane_mcp.moneta.inject`.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger

from plane_mcp.moneta import workspace as ws

logger = get_logger(__name__)


def register_moneta_tools(mcp: FastMCP) -> None:
    """Register the fork's extra tools on the given MCP instance."""

    @mcp.tool()
    def list_workspaces() -> list[dict[str, Any]]:
        """
        List the Plane workspaces the authenticated user belongs to.

        Use this to discover workspace slugs, then pass ``workspace_slug`` to any
        workspace-scoped tool to act on a specific workspace. Backed by Plane's
        ``/api/users/me/workspaces/`` endpoint (identity comes from the SSO session).

        Returns:
            List of ``{id, name, slug}`` for each workspace the user can access.
        """
        return ws._fetch_workspaces()
