"""Static tool visibility for the Moneta fork.

Exposes a fixed, curated set of tools on ``tools/list`` — the standard MCPO
convention, where every listed tool becomes one generated HTTP endpoint. There
is no runtime discovery or enablement: what the server lists at startup is what
clients can call.

Tools outside :data:`ENABLED_TOOLS` stay registered in the provider (so they can
be re-activated later without code archaeology) but are hidden from ``tools/list``
and rejected on ``tools/call`` via FastMCP's visibility transform.

To activate more tools, either add the name to :data:`ENABLED_TOOLS` or set the
``PLANE_MCP_ENABLED_TOOLS`` env var to a comma-separated list (which *replaces*
the curated set; ``list_workspaces`` is always kept).

Called from ``plane_mcp.tools.register_tools`` via a single hook, which must run
LAST — after ``add_workspace_arg``, whose remove/add cycle would otherwise
discard the visibility filter.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

# Workspace discovery — required for every other tool to resolve a workspace,
# so it is always visible regardless of the curated set or the env override.
ALWAYS_ENABLED: frozenset[str] = frozenset({"list_workspaces"})

# The curated set exposed on tools/list. Everything else is registered but hidden.
ENABLED_TOOLS: frozenset[str] = frozenset(
    {
        # Projects
        "get_project_members",
        "list_projects",
        "retrieve_project",
        "update_project",
        # Work items
        "add_work_item_assignee",
        "add_work_item_label",
        "create_work_item",
        "list_work_items",
        "list_workspace_work_items",
        "remove_work_item_assignee",
        "retrieve_work_item",
        "retrieve_work_item_by_identifier",
        "search_work_items",
        "update_work_item",
        # Work item comments
        "create_work_item_comment",
        "list_work_item_comments",
        "retrieve_work_item_comment",
        # Work item links
        "list_work_item_links",
        "retrieve_work_item_link",
        # Work item relations
        "create_work_item_relation",
        "list_work_item_relations",
        "remove_work_item_relation",
        # Cycles
        "add_work_items_to_cycle",
        "complete_cycle",
        "create_cycle",
        "list_cycle_work_items",
        "list_cycles",
        "retrieve_cycle",
        "transfer_cycle_work_items",
        "update_cycle",
        # Modules
        "add_work_items_to_module",
        "list_module_work_items",
        "list_modules",
        "retrieve_module",
        # Labels
        "list_labels",
        "retrieve_label",
        # States — needed so state_id on create/update_work_item is discoverable
        "list_states",
        "retrieve_state",
        # Users
        "get_me",
        "get_workspace_members",
        # PQL
        "get_pql_reference",
    }
)


def _parse_enabled_tools() -> set[str] | None:
    """Parse ``PLANE_MCP_ENABLED_TOOLS`` into a set of tool names.

    Returns None if the env var is unset or empty (meaning use ENABLED_TOOLS).
    """
    raw = os.environ.get("PLANE_MCP_ENABLED_TOOLS", "").strip()
    if not raw:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


def _get_all_tools(mcp: FastMCP) -> list[Tool]:
    """Return every registered Tool from the provider's internal store."""
    try:
        return [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError:
        logger.warning("_get_all_tools: cannot access tool registry")
        return []


def apply_tool_visibility(mcp: FastMCP) -> None:
    """Hide every tool outside the enabled set from ``tools/list``.

    Must be called AFTER all domain tools are registered and ``add_workspace_arg``
    has run — that transform removes and re-adds each tool, which would drop an
    earlier visibility filter.
    """
    env_override = _parse_enabled_tools()
    visible = (env_override if env_override is not None else set(ENABLED_TOOLS)) | ALWAYS_ENABLED

    mcp._local_provider.enable(names=visible, only=True, components={"tool"})

    all_names = {t.name for t in _get_all_tools(mcp)}
    unknown = visible - all_names
    if unknown:
        logger.warning("Tool visibility: unknown tool name(s) in enabled set: %s", ", ".join(sorted(unknown)))

    source = "PLANE_MCP_ENABLED_TOOLS" if env_override is not None else "ENABLED_TOOLS"
    logger.info(
        "Tool visibility: %d tool(s) exposed (%s), %d registered but hidden",
        len(visible & all_names),
        source,
        len(all_names - visible),
    )
