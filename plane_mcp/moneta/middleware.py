"""Action-argument validation that knows about the fork's injected arguments.

Upstream's ``ValidateActionArguments`` refuses any argument the chosen action does
not declare. :mod:`plane_mcp.moneta.inject` adds ``workspace_slug`` to every
workspace-scoped tool without touching its ``ACTIONS``, so upstream's check would
refuse every call that picks a workspace. This variant judges the call with the
injected arguments set aside and is otherwise upstream's check unchanged.

It keeps upstream's class name so ``plane_mcp.server`` only swaps the import: the
line that mounts it, and upstream's test that every transport mounts it, stay as
they are.
"""

from __future__ import annotations

from plane_mcp import middleware as upstream
from plane_mcp.moneta.inject import INJECTED_ARGS


class ValidateActionArguments(upstream.ValidateActionArguments):
    def rejection(self, tool: str, arguments: dict) -> str | None:
        declared = {name: value for name, value in arguments.items() if name not in INJECTED_ARGS}
        return super().rejection(tool, declared)
