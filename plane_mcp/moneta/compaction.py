"""Middleware that compacts every tool call: default list fields in, no nulls out (Moneta fork).

Mounted last by :func:`plane_mcp.moneta.surface.apply_moneta_surface`, so the argument
checks upstream's middleware makes see the call exactly as the caller sent it.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from plane_mcp.moneta.list_fields import with_default_fields
from plane_mcp.moneta.serialization import compacted_result
from plane_mcp.tools.registry import alias_table


class CompactResponses(Middleware):
    def __init__(self) -> None:
        self._aliases = alias_table()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        arguments = getattr(context.message, "arguments", None)
        if isinstance(arguments, dict):
            context.message.arguments = with_default_fields(self._operation(context.message.name, arguments), arguments)
        return compacted_result(await call_next(context))

    def _operation(self, name: str, arguments: dict[str, Any]) -> tuple[str, str | None]:
        action = arguments.get("action")
        return self._aliases.get(name) or (name, action if isinstance(action, str) else None)
