"""Self-hosted trims to upstream's tool descriptions and arguments (Moneta fork).

Upstream describes Plane Cloud. Some of what it advertises has no counterpart on
the self-hosted ``/api/v1``, and an agent that reads it will either call a 404 or
pass a filter that is silently dropped. :func:`apply_self_hosted_trims` fixes both
from here, so the resource modules stay byte-for-byte upstream:

* :data:`UNSUPPORTED_ARGUMENTS` are removed from the action lines (and from the
  listed schema, by :mod:`plane_mcp.moneta.listing`), and a call that supplies one
  is refused with the reason. The
  self-hosted ``members/`` route (``WorkspaceMemberAPIEndpoint.get``) reads no
  query params and returns ``UserLiteSerializer`` fields plus an integer ``role``,
  so its role-slug, activity and bot filters and its paging cannot be honoured;
  custom relation definitions do not exist at all.
* :data:`DESCRIPTION_REWRITES` replace sentences about features the server lacks:
  the workspace state catalogue, role definitions, custom relation definitions, and
  label fields that only hidden actions use. Work item ``type_id`` stays: the
  self-hosted ``IssueSerializer`` accepts it on create and update.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger

from plane_mcp.tools import label as label_tool
from plane_mcp.tools import member as member_tool
from plane_mcp.tools import state as state_tool

logger = get_logger(__name__)

_NO_MEMBER_FLAGS = (
    "this Plane server's member list carries no role slug, activity or bot flag, so it cannot filter on them; "
    "each member's integer role is in the result"
)
_ONE_MEMBER_PAGE = "this Plane server returns every member in one unordered page"
_NO_CUSTOM_RELATIONS = "custom relation definitions are not available on this Plane server; pass relation_type"

UNSUPPORTED_ARGUMENTS: Mapping[str, Mapping[str, str]] = {
    "member": {
        "role_slug": _NO_MEMBER_FLAGS,
        "is_active": _NO_MEMBER_FLAGS,
        "is_bot": _NO_MEMBER_FLAGS,
        "cursor": _ONE_MEMBER_PAGE,
        "per_page": _ONE_MEMBER_PAGE,
        "order_by": _ONE_MEMBER_PAGE,
    },
    "workitem_relation": {
        "relation_definition_id": _NO_CUSTOM_RELATIONS,
        "relation_definition_label": _NO_CUSTOM_RELATIONS,
    },
}

STATE_FOOTER = (
    "States belong to a project, so project_id is required. Each project also has a triage state, "
    "which Plane owns and does not list."
)

DESCRIPTION_REWRITES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "state": (
        (
            "list (no required params; optional project_id, cursor, per_page) -- workspace scope when "
            "project_id is omitted",
            "list (project_id; optional cursor, per_page)",
        ),
        ("retrieve (state_id; optional project_id)", "retrieve (project_id, state_id)"),
        (state_tool.FOOTER, STATE_FOOTER),
    ),
    "member": (
        ("Workspace and project members, and role definitions.", "Workspace and project members."),
        (" -- name filters match case-insensitively and combine with AND", " -- filters match case-insensitively"),
        (f"\n\n{member_tool.FOOTER}", ""),
    ),
    "label": ((f"\n\n{label_tool.FOOTER}", ""),),
    "workitem_relation": (
        ("Relations between work items, and the definitions that type them.", "Relations between work items."),
    ),
}


def _optional_patterns(name: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    escaped = re.escape(name)
    return (
        (re.compile(rf"; optional {escaped}\)"), ")"),
        (re.compile(rf"\b{escaped}, "), ""),
        (re.compile(rf", {escaped}\b"), ""),
    )


def without_optional_arguments(description: str, names: frozenset[str]) -> str:
    """Remove ``names`` from the optional-argument lists of a ``build_description`` text."""
    for name in sorted(names):
        for pattern, replacement in _optional_patterns(name):
            description = pattern.sub(replacement, description)
    return description


def rewritten(description: str, rewrites: tuple[tuple[str, str], ...]) -> str:
    for old, new in rewrites:
        description = description.replace(old, new)
    return description


def _is_supplied(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return value not in (None, "", 0, [])


def _unsupported_guard(reasons: Mapping[str, str]):
    async def guard(**kwargs: Any):
        supplied = sorted(name for name in reasons if _is_supplied(kwargs.get(name)))
        if supplied:
            raise ToolError(f"Not supported: {', '.join(supplied)} ({reasons[supplied[0]]}).")
        return await forward(**kwargs)

    return guard


def trimmed(tool: Tool) -> Tool:
    """The tool with this server's unsupported arguments refused and its description corrected."""
    reasons = UNSUPPORTED_ARGUMENTS.get(tool.name, {})
    description = without_optional_arguments(
        rewritten(tool.description or "", DESCRIPTION_REWRITES.get(tool.name, ())), frozenset(reasons)
    )
    if not reasons:
        return Tool.from_tool(tool, description=description)
    return Tool.from_tool(tool, transform_fn=_unsupported_guard(reasons), description=description)


def _needs_trim(tool: Tool) -> bool:
    if tool.name not in UNSUPPORTED_ARGUMENTS and tool.name not in DESCRIPTION_REWRITES:
        return False
    advertised = set((tool.parameters or {}).get("properties", {}))
    return bool(advertised & set(UNSUPPORTED_ARGUMENTS.get(tool.name, {}))) or any(
        old in (tool.description or "") for old, _ in DESCRIPTION_REWRITES.get(tool.name, ())
    )


def apply_self_hosted_trims(mcp: FastMCP) -> None:
    """Correct the descriptions and refuse the arguments this server cannot honour."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("apply_self_hosted_trims: cannot access the tool registry (%s)", exc)
        return

    for tool in [trimmed(tool) for tool in tools if _needs_trim(tool)]:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(tool)
