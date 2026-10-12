"""The Moneta fork's tool surface, applied on top of upstream's ``register_tools``.

One hook in ``plane_mcp.server._configured`` (shared by every transport) and the
Cognito server in :mod:`plane_mcp.moneta.http` call :func:`apply_moneta_surface`
after upstream has registered its resource tools. Keeping the fork out of
``register_tools`` itself leaves upstream's catalogue tests exercising the
catalogue they were written for.

Order matters: ``disable_pql``, ``restore_relation_types`` and ``add_workspace_arg``
each remove and re-add the tools they transform, which would discard a visibility filter applied earlier, so
``apply_tool_visibility`` runs last.
"""

from __future__ import annotations

from fastmcp import FastMCP

from plane_mcp.moneta.inject import add_workspace_arg
from plane_mcp.moneta.instructions import MONETA_INSTRUCTIONS
from plane_mcp.moneta.pql_guard import disable_pql
from plane_mcp.moneta.relations import restore_relation_types
from plane_mcp.moneta.tools import register_moneta_tools
from plane_mcp.moneta.visibility import apply_tool_visibility


def apply_moneta_surface(mcp: FastMCP) -> FastMCP:
    """Add the fork's tools, guards and curation to an upstream-configured server."""
    register_moneta_tools(mcp)
    disable_pql(mcp)
    restore_relation_types(mcp)
    add_workspace_arg(mcp)
    apply_tool_visibility(mcp)
    mcp.instructions = MONETA_INSTRUCTIONS
    return mcp
