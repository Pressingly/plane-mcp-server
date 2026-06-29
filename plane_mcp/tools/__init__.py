"""Tools for Plane MCP Server."""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from plane_mcp.moneta.inject import add_workspace_arg
from plane_mcp.moneta.tools import register_moneta_tools
from plane_mcp.tools.cycles import register_cycle_tools
from plane_mcp.tools.initiatives import register_initiative_tools
from plane_mcp.tools.intake import register_intake_tools
from plane_mcp.tools.labels import register_label_tools
from plane_mcp.tools.milestones import register_milestone_tools
from plane_mcp.tools.modules import register_module_tools
from plane_mcp.tools.pages import register_page_tools
from plane_mcp.tools.pql import register_pql_tools
from plane_mcp.tools.projects import register_project_tools
from plane_mcp.tools.states import register_state_tools
from plane_mcp.tools.users import register_user_tools
from plane_mcp.tools.work_item_activities import register_work_item_activity_tools
from plane_mcp.tools.work_item_comments import register_work_item_comment_tools
from plane_mcp.tools.work_item_links import register_work_item_link_tools
from plane_mcp.tools.work_item_relations import register_work_item_relation_tools
from plane_mcp.tools.work_items import register_work_item_tools
from plane_mcp.tools.workspaces import register_workspace_tools

logger = logging.getLogger(__name__)


def _parse_enabled_tools() -> set[str] | None:
    """Parse PLANE_MCP_ENABLED_TOOLS env var into a set of tool names.

    Returns None if the env var is unset or empty (meaning all tools are enabled).
    """
    raw = os.environ.get("PLANE_MCP_ENABLED_TOOLS", "").strip()
    if not raw:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


def _filter_tools(mcp: FastMCP, enabled: set[str]) -> None:
    """Remove any registered tools not in the *enabled* set."""
    try:
        components = list(mcp._local_provider._components.values())
    except AttributeError as exc:
        logger.warning("_filter_tools: cannot access tool registry (%s) — skipping filter", exc)
        return

    registered = [c.name for c in components if isinstance(c, Tool)]
    removed = []
    for name in registered:
        if name not in enabled:
            mcp._local_provider.remove_tool(name)
            removed.append(name)

    if removed:
        logger.info("PLANE_MCP_ENABLED_TOOLS: removed %d tool(s) not in allow-list: %s", len(removed), ", ".join(sorted(removed)))


def register_tools(mcp: FastMCP) -> None:
    """Register all tools with the MCP server."""
    register_project_tools(mcp)
    register_work_item_tools(mcp)
    register_work_item_activity_tools(mcp)
    register_work_item_comment_tools(mcp)
    register_work_item_link_tools(mcp)
    register_work_item_relation_tools(mcp)
    register_cycle_tools(mcp)
    register_user_tools(mcp)
    register_module_tools(mcp)
    register_initiative_tools(mcp)
    register_intake_tools(mcp)
    register_label_tools(mcp)
    register_page_tools(mcp)
    register_state_tools(mcp)
    register_workspace_tools(mcp)
    register_milestone_tools(mcp)
    register_pql_tools(mcp)
    register_moneta_tools(mcp)  # Moneta fork: list_workspaces
    add_workspace_arg(mcp)  # Moneta fork: inject optional workspace_slug on every workspace-scoped tool

    # Filter tools based on PLANE_MCP_ENABLED_TOOLS env var
    enabled = _parse_enabled_tools()
    if enabled is not None:
        _filter_tools(mcp, enabled)
        logger.info("PLANE_MCP_ENABLED_TOOLS: %d tool(s) in allow-list", len(enabled))
    else:
        # Count registered tools for startup log
        try:
            tool_count = sum(1 for c in mcp._local_provider._components.values() if isinstance(c, Tool))
            logger.info("All %d tools registered (PLANE_MCP_ENABLED_TOOLS not set)", tool_count)
        except AttributeError:
            logger.info("All tools registered (PLANE_MCP_ENABLED_TOOLS not set)")
