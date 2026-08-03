"""Inject an optional ``workspace_slug`` arg onto every workspace-scoped tool.

Rather than editing the ~144 upstream tool signatures (the maximal merge-conflict
surface), we transform each registered tool at startup with FastMCP's public
tool-transform API (:meth:`Tool.from_tool` + :func:`forward`): a thin wrapper adds
an optional ``workspace_slug`` argument, stashes it in a ContextVar for the
duration of the call, and forwards to the original tool. ``get_plane_client_context``
reads that ContextVar via :func:`plane_mcp.moneta.workspace.resolve_workspace`.

Because the arg rides each individual ``tools/call`` request (its own ContextVar
scope), concurrent Claude conversations targeting different workspaces stay fully
isolated — unlike any server-side "active workspace" state, which would be shared
across a user's conversations.

The injection is applied from ``plane_mcp.tools.register_tools`` (one hook line),
so the per-domain tool modules stay byte-for-byte upstream.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger

from plane_mcp.moneta import workspace as ws

logger = get_logger(__name__)

# Moneta tools that are not workspace-scoped — leave them untouched.
_SKIP_TOOLS = frozenset({"list_workspaces"})

_WS_DESCRIPTION = (
    "Optional. The Plane workspace slug to act on (call list_workspaces to discover "
    "slugs). Defaults to the server's configured workspace; required when the server "
    "exposes multiple workspaces and none is configured."
)


async def _inject_workspace(workspace_slug: str | None = None, **kwargs: Any):
    """Transform wrapper: stash workspace_slug for the call, then run the parent tool."""
    token = ws.set_override(workspace_slug)
    try:
        return await forward(**kwargs)
    finally:
        ws.reset_override(token)


def add_workspace_arg(mcp: FastMCP) -> None:
    """Transform every workspace-scoped tool to accept an optional ``workspace_slug``.

    Idempotent (skips tools that already have the arg) and defensive (a failure on
    one tool logs and is skipped rather than breaking startup).
    """
    try:
        components = list(mcp._local_provider._components.values())
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("add_workspace_arg: cannot access the tool registry (%s) — workspace_slug not injected", exc)
        return

    injected = 0
    for component in components:
        if not isinstance(component, Tool) or component.name in _SKIP_TOOLS:
            continue
        if "workspace_slug" in (component.parameters or {}).get("properties", {}):
            continue
        try:
            transformed = Tool.from_tool(component, transform_fn=_inject_workspace)
            props = transformed.parameters.get("properties", {})
            if "workspace_slug" in props:
                props["workspace_slug"]["description"] = _WS_DESCRIPTION
            mcp._local_provider.remove_tool(component.name)
            mcp.add_tool(transformed)
            injected += 1
        except Exception as exc:  # pragma: no cover - defensive; never break startup
            logger.warning("add_workspace_arg: failed to inject workspace_slug into %s (%s)", component.name, exc)

    logger.debug("add_workspace_arg: injected workspace_slug into %d tool(s)", injected)
