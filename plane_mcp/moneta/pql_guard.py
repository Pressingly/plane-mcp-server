"""Reject PQL instead of silently ignoring it (Moneta fork).

Upstream targets Plane Cloud, whose work-item list endpoints evaluate a ``pql``
query param. Our self-hosted Plane's ``/api/v1`` list endpoints
(``IssueListCreateAPIEndpoint.get``, the cycle-issue and module-issue lists) read
no filter params at all, so plane-sdk's ``pql=`` is dropped server-side and the
call returns the unfiltered page as if it had matched.

:func:`disable_pql` transforms each resource tool that takes ``pql`` (``workitem``,
``cycle``, ``module``) so its ``pql`` argument is described as unsupported and any
non-empty value raises a ``ToolError`` rather than reaching Plane. It also strips
PQL from those tools' descriptions, so nothing clients see advertises it, along with
the pointer to the workspace-wide work-item list, which the self-hosted API lacks. The PQL
reference tool itself is hidden by :mod:`plane_mcp.moneta.visibility`. Applied from
:func:`plane_mcp.moneta.surface.apply_moneta_surface`, so the tool modules stay
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

from plane_mcp.pql_reference import PQL_FIELD_HINT

logger = get_logger(__name__)

PQL_TOOLS = frozenset({"workitem", "cycle", "module"})

PQL_UNSUPPORTED_DESCRIPTION = (
    "Not supported by this Plane server: leave unset. Passing a value returns an error, "
    "because the server would ignore it and return unfiltered results."
)

PQL_UNSUPPORTED_ERROR = (
    "PQL filtering is not supported by this self-hosted Plane server: it ignores the pql "
    "argument and would return the unfiltered list. Call the action again without pql and "
    "filter the returned items yourself (each carries assignees, labels, priority and "
    "state; pass expand=state to get the state group), using order_by and cursor to page."
)

_UNSCOPED_LIST = (
    ("  list (no required params; optional project_id, ", "  list (project_id; optional "),
    (" -- omit project_id to search the whole workspace", ""),
)

_PQL_IN_OPTIONAL = (
    (re.compile(r"; optional pql\)"), ")"),
    (re.compile(r"\bpql, "), ""),
    (re.compile(r", pql\b"), ""),
)


def _without_pql_param(line: str) -> str:
    for pattern, replacement in _PQL_IN_OPTIONAL:
        line = pattern.sub(replacement, line)
    return line


def strip_pql_mentions(description: str) -> str:
    """Remove PQL guidance from a tool description: the field hint, the ``pql`` param, any line naming PQL.

    The workspace-wide list goes with it: on this server it is a 404, not a search, so the
    ``list`` line names project_id as required.
    """
    text = description.replace(f" {PQL_FIELD_HINT}", "").replace(PQL_FIELD_HINT, "")
    for unscoped, scoped in _UNSCOPED_LIST:
        text = text.replace(unscoped, scoped)
    lines = (_without_pql_param(line) for line in text.split("\n"))
    return "\n".join(line for line in lines if "PQL" not in line).strip()


def _is_set(pql: str | None) -> bool:
    return bool(pql and pql.strip())


async def _reject_pql(pql: str | None = None, **kwargs: Any):
    if _is_set(pql):
        raise ToolError(PQL_UNSUPPORTED_ERROR)
    return await forward(**kwargs)


def _takes_unguarded_pql(tool: Tool) -> bool:
    pql = (tool.parameters or {}).get("properties", {}).get("pql")
    return tool.name in PQL_TOOLS and pql is not None and pql.get("description") != PQL_UNSUPPORTED_DESCRIPTION


def _guarded(tool: Tool) -> Tool:
    return Tool.from_tool(
        tool,
        transform_fn=_reject_pql,
        description=strip_pql_mentions(tool.description or ""),
        transform_args={"pql": ArgTransform(description=PQL_UNSUPPORTED_DESCRIPTION)},
    )


def disable_pql(mcp: FastMCP) -> None:
    """Make the PQL-taking resource tools reject ``pql`` and drop PQL from their descriptions."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("disable_pql: cannot access the tool registry (%s) — pql left enabled", exc)
        return

    rewritten = [_guarded(tool) for tool in tools if _takes_unguarded_pql(tool)]
    for tool in rewritten:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(tool)

    logger.debug("disable_pql: rewrote %d tool(s)", len(rewritten))
