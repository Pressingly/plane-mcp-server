"""Tool discovery and dynamic enablement for the Moneta fork.

Registers two meta tools (``list_available_tools`` and ``enable_tools``) that
let an LLM discover the full tool catalog and selectively enable tools at
runtime. On startup only a small default set is visible; everything else is
disabled via FastMCP's visibility transforms but remains in the registry for
discovery and on-demand activation.

Called from ``plane_mcp.tools.register_tools`` via two hooks so the upstream
per-domain tool modules stay untouched:

1. ``build_tool_catalog(mcp)`` — snapshot tool metadata BEFORE workspace_slug
   injection (tools still carry their original ``__module__``).
2. ``register_discovery_tools(mcp)`` — register meta tools and apply default
   visibility AFTER all transforms.
"""

from __future__ import annotations

import os

from fastmcp import Context, FastMCP
from fastmcp.tools.base import Tool
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

# ── defaults ────────────────────────────────────────────────────────────────

DEFAULT_TOOLS: frozenset[str] = frozenset({
    "list_projects",
    "list_work_items",
    "create_work_item",
    "update_work_item",
    "search_work_items",
})

# Meta tools are always visible — never disabled.
META_TOOLS: frozenset[str] = frozenset({
    "list_available_tools",
    "enable_tools",
    "list_workspaces",
})

# Module categories for display grouping in list_available_tools.
# Order here determines display order.
MODULE_CATEGORIES: list[tuple[str, str]] = [
    ("projects", "Projects"),
    ("work_items", "Work Items"),
    ("work_item_activities", "Work Item Activities"),
    ("work_item_comments", "Work Item Comments"),
    ("work_item_links", "Work Item Links"),
    ("work_item_relations", "Work Item Relations"),
    ("cycles", "Cycles"),
    ("modules", "Modules"),
    ("initiatives", "Initiatives"),
    ("intake", "Intake"),
    ("labels", "Labels"),
    ("milestones", "Milestones"),
    ("pages", "Pages"),
    ("states", "States"),
    ("users", "Users"),
    ("workspaces", "Workspaces"),
    ("pql", "PQL"),
    ("moneta", "Moneta"),
]

# Tool name -> category key (built at startup by build_tool_catalog).
_tool_to_category: dict[str, str] = {}

# Tool name -> description (built at startup by build_tool_catalog).
_tool_descriptions: dict[str, str] = {}

# Tools currently enabled at the global (provider) level.
# Session-level enables (via enable_tools) are additive on top of this
# and are tracked per-session by FastMCP's visibility transforms.
_globally_enabled: set[str] = set()


def _parse_enabled_tools() -> set[str] | None:
    """Parse ``PLANE_MCP_ENABLED_TOOLS`` env var into a set of tool names.

    Returns None if the env var is unset or empty (meaning use defaults).
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


# ── module suffix -> category key ──────────────────────────────────────────

_MODULE_SUFFIX_MAP: dict[str, str] = {
    "tools.projects": "projects",
    "tools.work_items": "work_items",
    "tools.work_item_activities": "work_item_activities",
    "tools.work_item_comments": "work_item_comments",
    "tools.work_item_links": "work_item_links",
    "tools.work_item_relations": "work_item_relations",
    "tools.cycles": "cycles",
    "tools.modules": "modules",
    "tools.initiatives": "initiatives",
    "tools.intake": "intake",
    "tools.labels": "labels",
    "tools.milestones": "milestones",
    "tools.pages": "pages",
    "tools.states": "states",
    "tools.users": "users",
    "tools.workspaces": "workspaces",
    "tools.pql": "pql",
    "moneta.tools": "moneta",
    "moneta.discovery": "moneta",
}


def build_tool_catalog(mcp: FastMCP) -> None:
    """Snapshot tool metadata (name, description, category) from registered tools.

    **Must be called BEFORE** ``add_workspace_arg`` — the workspace injection
    replaces each tool's ``fn`` with a wrapper whose ``__module__`` is
    ``plane_mcp.moneta.inject``, losing the original module path needed for
    category inference.
    """
    for tool in _get_all_tools(mcp):
        name = tool.name
        desc = (tool.description or "").split("\n")[0].strip()
        _tool_descriptions[name] = desc

        # Infer category from the tool function's module path
        fn = getattr(tool, "fn", None)
        module = getattr(fn, "__module__", "") if fn else ""
        category = "other"
        for suffix, cat in _MODULE_SUFFIX_MAP.items():
            if module.endswith(suffix):
                category = cat
                break
        _tool_to_category[name] = category

    logger.debug("build_tool_catalog: cataloged %d tool(s)", len(_tool_descriptions))


def _apply_default_visibility(mcp: FastMCP) -> None:
    """Disable all tools except the startup set and meta tools."""
    global _globally_enabled

    env_override = _parse_enabled_tools()
    startup_set = env_override if env_override is not None else DEFAULT_TOOLS

    visible = startup_set | META_TOOLS
    _globally_enabled = set(visible)

    # Use FastMCP's visibility transform: enable only the visible set.
    mcp._local_provider.enable(names=visible, only=True, components={"tool"})

    all_names = {t.name for t in _get_all_tools(mcp)}
    disabled_count = len(all_names - visible)
    source = "PLANE_MCP_ENABLED_TOOLS" if env_override is not None else "defaults"
    logger.info(
        "Tool discovery: %d tool(s) enabled (%s), %d disabled but discoverable",
        len(visible & all_names),
        source,
        disabled_count,
    )


def register_discovery_tools(mcp: FastMCP) -> None:
    """Register the two meta tools and apply default visibility.

    Must be called AFTER all domain tools and ``add_workspace_arg`` have been
    registered (and after ``build_tool_catalog`` has snapshotted metadata).
    """

    @mcp.tool()
    def list_available_tools() -> str:
        """List ALL available tools on this Plane MCP server, grouped by category.

        Shows which tools are currently enabled and which can be activated
        with the enable_tools tool. Use this to discover tools you need
        before enabling them.

        Returns:
            A formatted catalog of all tools with their descriptions and
            enabled/disabled status.
        """
        category_map: dict[str, str] = dict(MODULE_CATEGORIES)
        groups: dict[str, list[str]] = {}

        for tool in _get_all_tools(mcp):
            name = tool.name
            if name in META_TOOLS:
                continue
            cat_key = _tool_to_category.get(name, "other")
            cat_label = category_map.get(cat_key, cat_key.replace("_", " ").title())
            enabled = name in _globally_enabled
            desc = _tool_descriptions.get(name, "")
            status = " (ENABLED)" if enabled else ""
            line = f"- {name}: {desc}{status}"
            groups.setdefault(cat_label, []).append(line)

        lines: list[str] = ["Available Plane MCP tools:\n"]
        ordered_labels = [label for _, label in MODULE_CATEGORIES]
        for label in ordered_labels:
            if label in groups:
                lines.append(f"## {label}")
                lines.extend(sorted(groups.pop(label)))
                lines.append("")
        for label in sorted(groups):
            lines.append(f"## {label}")
            lines.extend(sorted(groups[label]))
            lines.append("")

        lines.append("Use enable_tools to activate any disabled tools.")
        return "\n".join(lines)

    @mcp.tool()
    async def enable_tools(tool_names: list[str], ctx: Context) -> str:
        """Enable additional tools on this MCP server so you can call them.

        After calling this, the newly enabled tools will appear in your tool
        list. Use list_available_tools first to see what's available.

        Args:
            tool_names: List of tool names to enable (e.g. ["list_cycles", "create_cycle"]).

        Returns:
            Confirmation of which tools were enabled.
        """
        all_tool_names = {t.name for t in _get_all_tools(mcp)}
        valid = [n for n in tool_names if n in all_tool_names]
        invalid = [n for n in tool_names if n not in all_tool_names]

        if not valid:
            msg = "No valid tool names provided."
            if invalid:
                msg += f" Unknown tools: {', '.join(invalid)}"
            return msg

        # Session-scoped enable — sends ToolListChangedNotification to the
        # calling client so the LLM sees the newly available tools.
        await ctx.enable_components(names=set(valid), components={"tool"})

        parts = [f"Enabled {len(valid)} tool(s): {', '.join(sorted(valid))}"]
        if invalid:
            parts.append(f"Unknown (skipped): {', '.join(sorted(invalid))}")
        parts.append("These tools are now available in your tool list.")
        return " ".join(parts)

    # Catalog already built by build_tool_catalog(); add the meta tools to it.
    for tool in _get_all_tools(mcp):
        if tool.name in META_TOOLS and tool.name not in _tool_descriptions:
            desc = (tool.description or "").split("\n")[0].strip()
            _tool_descriptions[tool.name] = desc
            _tool_to_category[tool.name] = "moneta"

    # Apply default visibility (disable everything except startup set + meta)
    _apply_default_visibility(mcp)
