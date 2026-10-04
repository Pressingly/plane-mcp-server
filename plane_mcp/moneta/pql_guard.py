"""Reject PQL on the list tools instead of silently ignoring it (Moneta fork).

Upstream targets Plane Cloud, whose work-item list endpoints evaluate a ``pql``
query param. Our self-hosted Plane's ``/api/v1`` list endpoints
(``IssueListCreateAPIEndpoint.get``, the cycle-issue and module-issue lists) read
no filter params at all, so plane-sdk's ``pql=`` is dropped server-side and the
tool returns the unfiltered page as if it had matched.

:func:`disable_pql` transforms each PQL-taking list tool so its ``pql`` argument is
described as unsupported and any non-empty value raises a ``ToolError`` rather than
reaching Plane. Applied from ``plane_mcp.tools.register_tools`` (one hook line), so
the tool modules stay byte-for-byte upstream.
"""

from __future__ import annotations

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

PQL_UNSUPPORTED_NOTE = "PQL filtering is not available on this Plane server; the pql argument is rejected."

PQL_UNSUPPORTED_ERROR = (
    "PQL filtering is not supported by this self-hosted Plane server: it ignores the pql "
    "argument and would return the unfiltered list. Call the tool again without pql and "
    "filter the returned items yourself (each carries assignees, labels, priority and "
    "state; pass expand=state to get the state group), using order_by and cursor to page."
)


def _is_set(pql: str | None) -> bool:
    return bool(pql and pql.strip())


async def _reject_pql(pql: str | None = None, **kwargs: Any):
    if _is_set(pql):
        raise ToolError(PQL_UNSUPPORTED_ERROR)
    return await forward(**kwargs)


def _takes_unguarded_pql(tool: Tool) -> bool:
    pql = (tool.parameters or {}).get("properties", {}).get("pql")
    return tool.name in PQL_TOOLS and pql is not None and pql.get("description") != PQL_UNSUPPORTED_DESCRIPTION


def disable_pql(mcp: FastMCP) -> None:
    """Make every registered PQL list tool reject ``pql`` and advertise it as unsupported."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool) and _takes_unguarded_pql(c)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("disable_pql: cannot access the tool registry (%s) — pql left enabled", exc)
        return

    for tool in tools:
        transformed = Tool.from_tool(
            tool,
            transform_fn=_reject_pql,
            description=f"{tool.description or ''}\n\n{PQL_UNSUPPORTED_NOTE}".strip(),
            transform_args={"pql": ArgTransform(description=PQL_UNSUPPORTED_DESCRIPTION)},
        )
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(transformed)

    logger.debug("disable_pql: guarded %d tool(s)", len(tools))
