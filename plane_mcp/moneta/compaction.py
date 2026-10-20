"""Middleware that compacts every tool call: default list fields in, no nulls out (Moneta fork).

An attachment ``read`` returns the file's own text, so it is passed through untouched.

Mounted last by :func:`plane_mcp.moneta.surface.apply_moneta_surface`, so the argument
checks upstream's middleware makes see the call exactly as the caller sent it.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from plane_mcp.moneta.list_fields import with_default_fields
from plane_mcp.moneta.serialization import compacted_result
from plane_mcp.tools.registry import alias_table

VERBATIM_OPERATIONS = frozenset({("workitem_attachment", "read")})


class CompactResponses(Middleware):
    def __init__(self) -> None:
        self._aliases = alias_table()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        arguments = getattr(context.message, "arguments", None)
        operation = self._operation(context.message.name, arguments if isinstance(arguments, dict) else {})
        if operation in VERBATIM_OPERATIONS:
            return await call_next(context)
        if isinstance(arguments, dict):
            context.message.arguments = with_default_fields(operation, arguments)
        return compacted_result(await call_next(context))

    def _operation(self, name: str, arguments: dict[str, Any]) -> tuple[str, str | None]:
        action = arguments.get("action")
        return self._aliases.get(name) or (name, action if isinstance(action, str) else None)
