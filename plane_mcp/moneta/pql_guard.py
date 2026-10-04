"""Reject PQL on the list tools instead of silently ignoring it (Moneta fork).

Upstream targets Plane Cloud, whose work-item list endpoints evaluate a ``pql``
query param. Our self-hosted Plane's ``/api/v1`` list endpoints
(``IssueListCreateAPIEndpoint.get``, the cycle-issue and module-issue lists) read
no filter params at all, so plane-sdk's ``pql=`` is dropped server-side and the
tool returns the unfiltered page as if it had matched.

:func:`disable_pql` transforms each PQL-taking list tool so its ``pql`` argument is
described as unsupported and any non-empty value raises a ``ToolError`` rather than
reaching Plane. It also rewrites the descriptions clients see (those tools, and
``search_work_items``, which points at them) so nothing advertises PQL. Applied
from ``plane_mcp.tools.register_tools`` (one hook line), so the tool modules stay
byte-for-byte upstream.
"""

from __future__ import annotations

import re
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

PQL_TOOLS = frozenset(
    {
        "list_work_items",
        "list_workspace_work_items",
        "list_archived_work_items",
        "list_cycle_work_items",
        "list_module_work_items",
    }
)

PQL_UNSUPPORTED_DESCRIPTION = (
    "Not supported by this Plane server: leave unset. Passing a value returns an error, "
    "because the server would ignore it and return unfiltered results."
)

PQL_UNSUPPORTED_ERROR = (
    "PQL filtering is not supported by this self-hosted Plane server: it ignores the pql "
    "argument and would return the unfiltered list. Call the tool again without pql and "
    "filter the returned items yourself (each carries assignees, labels, priority and "
    "state; pass expand=state to get the state group), using order_by and cursor to page."
)

PQL_POINTING_TOOLS = frozenset({"search_work_items"})

_OPTIONAL_PQL_PHRASE = " with optional PQL filtering"
_PQL_POINTER = re.compile(r"For structured\s+filtering.*?PQL expression\.", re.DOTALL)
_PQL_ONLY_PASSAGES = (
    re.compile(r"For workspace-wide filtering use list_workspace_work_items instead\.\n?"),
    re.compile(r"For UUID fields.*?to get the UUID\.", re.DOTALL),
)
_NO_STRUCTURED_FILTERING = (
    "Structured filtering (priority, state, assignee, dates) is not available on this "
    "server: page through `list_work_items` and filter the results."
)


def strip_pql_mentions(description: str) -> str:
    """Remove PQL guidance from a tool description, including pointers to tools that only work with it.

    ``list_workspace_work_items`` is named here because the self-hosted API has no
    workspace-level list route, so pointing agents at it only produces a 404.
    """
    text = _PQL_POINTER.sub(_NO_STRUCTURED_FILTERING, description.replace(_OPTIONAL_PQL_PHRASE, ""))
    for passage in _PQL_ONLY_PASSAGES:
        text = passage.sub("", text)
    return "\n".join(line for line in text.splitlines() if "PQL" not in line).strip()


def _is_set(pql: str | None) -> bool:
    return bool(pql and pql.strip())


async def _reject_pql(pql: str | None = None, **kwargs: Any):
    if _is_set(pql):
        raise ToolError(PQL_UNSUPPORTED_ERROR)
    return await forward(**kwargs)


def _takes_unguarded_pql(tool: Tool) -> bool:
    pql = (tool.parameters or {}).get("properties", {}).get("pql")
    return tool.name in PQL_TOOLS and pql is not None and pql.get("description") != PQL_UNSUPPORTED_DESCRIPTION


def _points_at_pql(tool: Tool) -> bool:
    return tool.name in PQL_POINTING_TOOLS and "PQL" in (tool.description or "")


def _guarded(tool: Tool) -> Tool:
    return Tool.from_tool(
        tool,
        transform_fn=_reject_pql,
        description=strip_pql_mentions(tool.description or ""),
        transform_args={"pql": ArgTransform(description=PQL_UNSUPPORTED_DESCRIPTION)},
    )


def _redescribed(tool: Tool) -> Tool:
    return Tool.from_tool(tool, description=strip_pql_mentions(tool.description or ""))


def _rewrite(tool: Tool) -> Tool | None:
    if _takes_unguarded_pql(tool):
        return _guarded(tool)
    if _points_at_pql(tool):
        return _redescribed(tool)
    return None


def disable_pql(mcp: FastMCP) -> None:
    """Make the PQL list tools reject ``pql`` and drop PQL from every description clients see."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("disable_pql: cannot access the tool registry (%s) — pql left enabled", exc)
        return

    rewritten = [replacement for replacement in map(_rewrite, tools) if replacement is not None]
    for tool in rewritten:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(tool)

    logger.debug("disable_pql: rewrote %d tool(s)", len(rewritten))
