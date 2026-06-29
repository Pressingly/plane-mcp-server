"""Tools for Plane MCP Server."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from plane_mcp.moneta.discovery import build_tool_catalog, register_discovery_tools
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

    # Moneta fork: snapshot tool catalog BEFORE workspace_slug injection
    # (tools still carry their original __module__ for category inference).
    build_tool_catalog(mcp)

    add_workspace_arg(mcp)  # Moneta fork: inject optional workspace_slug on every workspace-scoped tool

    # Moneta fork: register meta tools (list_available_tools, enable_tools),
    # and apply default visibility so only the startup set is exposed to the
    # LLM. Must run last — after all tools are registered and transformed.
    register_discovery_tools(mcp)
