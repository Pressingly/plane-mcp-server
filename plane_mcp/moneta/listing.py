"""Trim each advertised input schema to the arguments this server honours (Moneta fork).

A curated resource keeps upstream's full parameter list at execution: every
transform in the chain forwards through it, and an argument removed from a tool's
schema would be refused as unexpected before the guard that explains why could
answer. So the trimming happens here, in ``tools/list`` only, the way upstream's
``StripOutputSchemas`` drops output schemas. A listed tool omits the arguments only
its hidden actions use and the arguments in
:data:`plane_mcp.moneta.trims.UNSUPPORTED_ARGUMENTS`; a call that sends one anyway
still reaches the guard that refuses it with a reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp.server.transforms import Transform
from fastmcp.tools.base import Tool

from plane_mcp.moneta.trims import UNSUPPORTED_ARGUMENTS
from plane_mcp.moneta.visibility import ALWAYS_ADVERTISED, catalogue, declared_arguments


def without_arguments(parameters: Mapping[str, Any], names: frozenset[str]) -> dict[str, Any]:
    """A copy of an input schema without ``names``."""
    properties = {name: spec for name, spec in parameters.get("properties", {}).items() if name not in names}
    required = [name for name in parameters.get("required", []) if name not in names]
    return {**parameters, "properties": properties, "required": required}


class AdvertisedArguments(Transform):
    def __init__(self, enabled: Mapping[str, frozenset[str]]) -> None:
        resources = catalogue()
        self._dropped = {
            name: self._unadvertised(name, actions, frozenset(resources.get(name, ())))
            for name, actions in enabled.items()
        }

    @staticmethod
    def _unadvertised(name: str, actions: frozenset[str], declared: frozenset[str]) -> frozenset[str]:
        unsupported = frozenset(UNSUPPORTED_ARGUMENTS.get(name, {}))
        if actions >= declared:
            return unsupported
        every = declared_arguments(name, declared)
        kept = declared_arguments(name, actions) | ALWAYS_ADVERTISED
        return (every - kept) | unsupported

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._trimmed(tool) for tool in tools]

    def _trimmed(self, tool: Tool) -> Tool:
        dropped = self._dropped.get(tool.name)
        if not dropped:
            return tool
        return tool.model_copy(update={"parameters": without_arguments(tool.parameters or {}, dropped)})
