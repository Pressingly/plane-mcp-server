"""Tools for Plane MCP Server."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from plane_mcp.moneta.inject import add_workspace_arg
from plane_mcp.moneta.tools import register_moneta_tools
from plane_mcp.moneta.visibility import apply_tool_visibility
from plane_mcp.tools.cycles import register_cycle_tools
from plane_mcp.tools.initiatives import register_initiative_tools
from plane_mcp.tools.intake import register_intake_tools
from plane_mcp.tools.labels import register_label_tools
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
    # Moneta fork: milestones are not supported by the Plane community edition,
    # so register_milestone_tools is intentionally not called. tools/milestones.py
    # is left byte-pristine for a future upstream pull.
    register_pql_tools(mcp)
    register_moneta_tools(mcp)  # Moneta fork: list_workspaces

    add_workspace_arg(mcp)  # Moneta fork: inject optional workspace_slug on every workspace-scoped tool

    # Moneta fork: expose only the curated tool set on tools/list. Must run last —
    # add_workspace_arg removes and re-adds each tool, which would drop the filter.
    apply_tool_visibility(mcp)
