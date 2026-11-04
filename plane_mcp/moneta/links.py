"""Web-only link URLs on ``workitem_link`` (Moneta fork).

The self-hosted API validates the scheme on link create but not on update:
``IssueLinkDetailAPIEndpoint.patch`` saves any string into ``IssueLink.url``, and the
web app renders it as an ``href``. :func:`guard_link_urls` refuses a ``url`` that is
not a plain ``http(s)://`` address on every action, so an agent cannot store a
``javascript:`` or ``data:`` link, and the tool's "url must be http:// or https://"
footer holds for update too. The resource module stays byte-for-byte upstream.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger

from plane_mcp.tools import workitem_link as link_tool

logger = get_logger(__name__)

WEB_SCHEMES = frozenset({"http", "https"})

_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")


def is_web_url(url: str) -> bool:
    """True for an ``http(s)://host...`` URL with no whitespace or control characters."""
    if _CONTROL_OR_SPACE.search(url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in WEB_SCHEMES and bool(parts.netloc)


async def _require_web_url(**kwargs: Any):
    url = kwargs.get("url") or ""
    if url and not is_web_url(url):
        raise ToolError("url must be an http:// or https:// address with no spaces, e.g. https://example.com/spec.")
    return await forward(**kwargs)


def guard_link_urls(mcp: FastMCP) -> None:
    """Refuse non-web link URLs on ``workitem_link`` create and update."""
    try:
        tool = next(
            c for c in mcp._local_provider._components.values() if isinstance(c, Tool) and c.name == link_tool.NAME
        )
    except (AttributeError, StopIteration) as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("guard_link_urls: cannot find the %s tool (%r)", link_tool.NAME, exc)
        return

    mcp._local_provider.remove_tool(tool.name)
    mcp.add_tool(Tool.from_tool(tool, transform_fn=_require_web_url))
