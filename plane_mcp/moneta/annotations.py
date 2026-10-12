"""Label every tool as a read or a write (Moneta fork).

MCP clients read a tool's ``readOnlyHint`` / ``destructiveHint`` annotations to
decide whether a call needs the user's confirmation before it runs, and treat a
tool that carries neither as a write. Upstream publishes no annotations, so
without this every lookup would be confirmed like a deletion.

The label is derived from the tool's name, which upstream keeps to a fixed verb
vocabulary. A name that matches no known verb is labelled a destructive write,
so a tool added by an upstream pull is confirmed until it is classified here.

Applied from ``plane_mcp.tools.register_tools`` (one hook line), so the tool
modules stay byte-for-byte upstream.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp.utilities.logging import get_logger
from mcp.types import ToolAnnotations

logger = get_logger(__name__)

# Reads data and changes nothing.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# Creates something new and leaves existing data as it was.
ADDITIVE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

# Can change or remove data that already exists.
DESTRUCTIVE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

READ_PREFIXES = ("get_", "list_", "retrieve_", "search_")
ADDITIVE_PREFIXES = ("create_", "add_", "attach_")


def annotations_for(tool_name: str) -> ToolAnnotations:
    """The read/write label for a tool, from the verb its name starts with."""
    if tool_name.startswith(READ_PREFIXES):
        return READ_ONLY
    if tool_name.startswith(ADDITIVE_PREFIXES):
        return ADDITIVE_WRITE
    return DESTRUCTIVE_WRITE


def apply_tool_annotations(mcp: FastMCP) -> None:
    """Set the read/write label on every registered tool, visible or hidden."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("apply_tool_annotations: cannot access the tool registry (%s) — tools left unlabelled", exc)
        return

    for tool in tools:
        tool.annotations = annotations_for(tool.name)

    logger.debug("apply_tool_annotations: labelled %d tool(s)", len(tools))
