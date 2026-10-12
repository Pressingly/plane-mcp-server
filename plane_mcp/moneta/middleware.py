"""Action-argument validation that knows about the fork's injected arguments.

Upstream's ``ValidateActionArguments`` refuses any argument the chosen action does
not declare. :mod:`plane_mcp.moneta.inject` adds ``workspace_slug`` to every
workspace-scoped tool without touching its ``ACTIONS``, so upstream's check would
refuse every call that picks a workspace. This variant judges the call with the
injected arguments set aside. A call that names no action is answered with the
actions this server actually offers (:func:`plane_mcp.moneta.visibility.enabled_actions`),
not upstream's full set; otherwise it is upstream's check unchanged.

It keeps upstream's class name so ``plane_mcp.server`` only swaps the import: the
line that mounts it, and upstream's test that every transport mounts it, stay as
they are.
"""

from __future__ import annotations

from plane_mcp import middleware as upstream
from plane_mcp.moneta.inject import INJECTED_ARGS
from plane_mcp.moneta.visibility import enabled_actions


class ValidateActionArguments(upstream.ValidateActionArguments):
    def __init__(self) -> None:
        super().__init__()
        self._offered = enabled_actions()

    def rejection(self, tool: str, arguments: dict) -> str | None:
        declared = {name: value for name, value in arguments.items() if name not in INJECTED_ARGS}
        if "action" not in declared and tool in self._offered and self._takes_an_action(tool):
            return upstream.missing_action_error(tool, self._offered[tool])
        return super().rejection(tool, declared)

    def _takes_an_action(self, tool: str) -> bool:
        """Upstream's test for a dispatch tool: a single-operation tool has no action to name."""
        return len(self._accepted.get(tool, {})) > 1
