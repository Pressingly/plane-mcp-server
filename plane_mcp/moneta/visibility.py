"""Static tool visibility for the Moneta fork.

Upstream advertises one action-dispatch tool per Plane resource (``workitem``,
``cycle``, ...). This fork exposes a curated subset of those resources and, inside
each one, a curated subset of its actions: the operations the self-hosted community
edition supports and that agents are trusted with. There is no runtime discovery or
enablement: what the server lists at startup is what clients can call.

* A resource outside :data:`ENABLED_ACTIONS` stays registered but is hidden from
  ``tools/list`` and rejected on ``tools/call`` via FastMCP's visibility transform.
* A listed resource advertises only its curated actions: the ``action`` enum and
  the description name nothing else, and any other action is refused at call time.
  That also covers upstream's retired tool names (``LegacyNames``), which resolve to
  a resource with the action pre-bound: ``delete_work_item`` reaches ``workitem``
  and is refused there, ``list_milestones`` resolves to a hidden resource and is
  unknown.

``PLANE_MCP_ENABLED_TOOLS`` (comma-separated) *replaces* the curated set. Each entry
is a resource name, which enables all of its actions, or a retired tool name such
as ``delete_work_item``, which enables the one action it maps to. ``list_workspaces``
is always kept.

Called from :func:`plane_mcp.moneta.surface.apply_moneta_surface`, which must run it
LAST: ``add_workspace_arg`` removes and re-adds each tool, which would discard the
visibility filter.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger

from plane_mcp.toolkit import action_names
from plane_mcp.tools.registry import RESOURCES, alias_table

logger = get_logger(__name__)

# Workspace discovery: required for every other tool to resolve a workspace, so it
# is always visible regardless of the curated set or the env override.
ALWAYS_ENABLED: frozenset[str] = frozenset({"list_workspaces"})

# The curated surface: resource tool -> the actions it exposes. Everything else is
# registered but hidden.
ENABLED_ACTIONS: Mapping[str, frozenset[str]] = {
    "project": frozenset({"list", "retrieve", "update"}),
    "workitem": frozenset(
        {
            "list",
            "retrieve",
            "retrieve_by_identifier",
            "search",
            "create",
            "update",
            "manage_assignee",
            "manage_label",
        }
    ),
    # update/delete exposed so the agent can correct or retract a comment it wrote itself.
    "workitem_comment": frozenset({"list", "retrieve", "create", "update", "delete"}),
    "workitem_link": frozenset({"list", "retrieve"}),
    # No delete: the self-hosted API has no route that removes a relation.
    "workitem_relation": frozenset({"list", "create"}),
    # manage_workitems is the only way to undo an add: transfer moves every
    # incomplete item, and no work-item field carries cycle or module.
    "cycle": frozenset(
        {"list", "retrieve", "create", "update", "list_workitems", "manage_workitems", "transfer_workitems", "complete"}
    ),
    "module": frozenset({"list", "retrieve", "list_workitems", "manage_workitems"}),
    "label": frozenset({"list", "retrieve"}),
    # Needed so the state id on workitem create/update is discoverable.
    "state": frozenset({"list", "retrieve"}),
    "member": frozenset({"me", "list_workspace", "list_project"}),
}

# Why an action is refused, where "not curated" would send the caller looking for a
# permission they cannot get.
REFUSED_ACTION_REASONS: Mapping[tuple[str, str], str] = {
    ("workitem_relation", "delete"): (
        "Removing a relation is not supported on this Plane server: its API has no route for it. "
        "Remove the relation in the Plane web app."
    ),
}

# Arguments every tool keeps whatever its actions declare.
ALWAYS_ADVERTISED = frozenset({"action", "workspace_slug"})

_ACTION_LINE = re.compile(r"^  (\w+) \(")


def catalogue() -> dict[str, tuple[str, ...]]:
    """Every registered resource tool and the actions it declares."""
    return {mod.NAME: action_names(mod.ACTIONS) for mod in RESOURCES}


def declared_arguments(tool_name: str, actions: frozenset[str]) -> frozenset[str]:
    """Every argument the given actions of a resource declare, required or optional."""
    declarations = next((mod.ACTIONS for mod in RESOURCES if mod.NAME == tool_name), ())
    return frozenset(
        name for action in declarations if action.name in actions for name in (*action.requires, *action.optional)
    )


def enabled_actions() -> dict[str, frozenset[str]]:
    """The surface in force: ``PLANE_MCP_ENABLED_TOOLS`` when set, else :data:`ENABLED_ACTIONS`."""
    override = parse_enabled_tools(os.environ.get("PLANE_MCP_ENABLED_TOOLS", ""))
    return override if override is not None else dict(ENABLED_ACTIONS)


def _expand_entry(
    entry: str, resources: Mapping[str, tuple[str, ...]], aliases: Mapping[str, tuple[str, str]]
) -> dict[str, frozenset[str]]:
    if entry in resources:
        return {entry: frozenset(resources[entry])}
    if entry in aliases:
        resource, action = aliases[entry]
        return {resource: frozenset({action})}
    return {}


def parse_enabled_tools(raw: str) -> dict[str, frozenset[str]] | None:
    """Turn a ``PLANE_MCP_ENABLED_TOOLS`` value into resource -> actions, or None when empty."""
    entries = [name.strip() for name in raw.split(",") if name.strip()]
    if not entries:
        return None
    resources, aliases = catalogue(), alias_table()
    enabled: dict[str, frozenset[str]] = {}
    unknown = []
    for entry in entries:
        expanded = _expand_entry(entry, resources, aliases)
        if not expanded and entry not in ALWAYS_ENABLED:
            unknown.append(entry)
        for resource, actions in expanded.items():
            enabled[resource] = enabled.get(resource, frozenset()) | actions
    if unknown:
        logger.warning("Tool visibility: unknown name(s) in PLANE_MCP_ENABLED_TOOLS: %s", ", ".join(sorted(unknown)))
    return enabled


def keep_action_lines(description: str, allowed: frozenset[str]) -> str:
    """Drop the ``Actions:`` lines of a ``build_description`` text whose action is not allowed."""
    lines = description.split("\n")
    kept = [line for line in lines if not (match := _ACTION_LINE.match(line)) or match.group(1) in allowed]
    action_rows = [i for i, line in enumerate(kept) if _ACTION_LINE.match(line)]
    return "\n".join(
        _punctuate(line, last=i == action_rows[-1]) if i in action_rows else line for i, line in enumerate(kept)
    )


def _punctuate(line: str, *, last: bool) -> str:
    body = line.rstrip(";.")
    return body + ("." if last else ";")


def refusal(tool_name: str, action: Any, allowed: frozenset[str]) -> str:
    """The message refusing an action this server does not offer."""
    reason = REFUSED_ACTION_REASONS.get((tool_name, action))
    if reason:
        return reason
    return f"Action {action!r} is not available on {tool_name} in this server. It takes: {', '.join(sorted(allowed))}."


def _action_guard(tool_name: str, allowed: frozenset[str]):
    async def guard(**kwargs: Any):
        action = kwargs.get("action")
        if action not in allowed:
            raise ToolError(refusal(tool_name, action, allowed))
        return await forward(**kwargs)

    return guard


def restrict_actions(tool: Tool, allowed: frozenset[str]) -> Tool:
    """The tool advertising and accepting only ``allowed`` of its actions."""
    declared = tuple((tool.parameters or {}).get("properties", {}).get("action", {}).get("enum") or ())
    kept = tuple(action for action in declared if action in allowed)
    restricted = Tool.from_tool(
        tool,
        transform_fn=_action_guard(tool.name, frozenset(kept)),
        description=keep_action_lines(tool.description or "", frozenset(kept)),
    )
    return restricted.model_copy(update={"parameters": with_action_enum(restricted.parameters, kept)})


def with_action_enum(parameters: Mapping[str, Any], actions: tuple[str, ...]) -> dict[str, Any]:
    """A copy of a tool's input schema whose ``action`` enum lists only ``actions``."""
    properties = parameters.get("properties", {})
    action = {key: value for key, value in properties.get("action", {}).items() if key != "const"}
    return {**parameters, "properties": {**properties, "action": {**action, "enum": list(actions)}}}


def _registered_tools(mcp: FastMCP) -> list[Tool]:
    try:
        return [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError:
        logger.warning("Tool visibility: cannot access the tool registry")
        return []


def _needs_restriction(tool: Tool, allowed: frozenset[str]) -> bool:
    declared = (tool.parameters or {}).get("properties", {}).get("action", {}).get("enum")
    return bool(declared) and not set(declared) <= allowed


def apply_tool_visibility(mcp: FastMCP) -> None:
    """Expose only the curated resources, each with only its curated actions."""
    enabled = enabled_actions()

    restricted = [
        restrict_actions(tool, enabled[tool.name])
        for tool in _registered_tools(mcp)
        if tool.name in enabled and _needs_restriction(tool, enabled[tool.name])
    ]
    for tool in restricted:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(tool)

    visible = set(enabled) | ALWAYS_ENABLED
    mcp._local_provider.enable(names=visible, only=True, components={"tool"})

    registered = {tool.name for tool in _registered_tools(mcp)}
    unknown = visible - registered
    if unknown:
        logger.warning("Tool visibility: unknown tool name(s) in enabled set: %s", ", ".join(sorted(unknown)))
    logger.info(
        "Tool visibility: %d tool(s) exposed (%s), %d registered but hidden",
        len(visible & registered),
        "PLANE_MCP_ENABLED_TOOLS" if os.environ.get("PLANE_MCP_ENABLED_TOOLS", "").strip() else "ENABLED_ACTIONS",
        len(registered - visible),
    )
