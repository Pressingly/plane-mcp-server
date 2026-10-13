"""Keep the fork's eight add/remove tool names callable (Moneta fork).

Upstream's ``LegacyNames`` resolves its own retired names, but these eight were
consolidated earlier upstream into ``manage_*`` tools and so have no alias there.
They were on this fork's curated list until the v0.3.4 sync, and callers (the
plane-tracker agent among them) still use them. :class:`MonetaAliases` resolves
each to its ``manage_*`` action with the argument names it shipped with, the way
``LegacyNames`` does: on ``tools/call`` only, never in ``tools/list``, and through
the curated resource tool, so the visibility guard still applies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward
from fastmcp.utilities.logging import get_logger

from plane_mcp.moneta.inject import INJECTED_ARGS

logger = get_logger(__name__)


@dataclass(frozen=True)
class Alias:
    """An old tool name: the resource action it maps to, and its argument names.

    ``renames`` maps the resource tool's argument to the name the old tool used.
    ``keeps`` are arguments the old tool shared with the resource under the same
    name. Everything else the resource takes is hidden. ``id_list`` names an old
    argument that took a list where the resource takes one string of ids.
    """

    resource: str
    action: str
    renames: Mapping[str, str]
    keeps: tuple[str, ...] = ("project_id",)
    id_list: str | None = None
    exposed: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exposed", frozenset(self.keeps) | frozenset(self.renames) | INJECTED_ARGS)


def _workitem(action: str, parent_arg: str, old_arg: str) -> Alias:
    return Alias("workitem", action, {"workitem_id": "work_item_id", parent_arg: old_arg})


def _container(resource: str, container_id: str, parent_arg: str, old_arg: str, *, many: bool) -> Alias:
    return Alias(
        resource,
        "manage_workitems",
        {parent_arg: old_arg},
        keeps=("project_id", container_id),
        id_list=old_arg if many else None,
    )


ALIASES: Mapping[str, Alias] = {
    "add_work_item_assignee": _workitem("manage_assignee", "add_user_id", "user_id"),
    "remove_work_item_assignee": _workitem("manage_assignee", "remove_user_id", "user_id"),
    "add_work_item_label": _workitem("manage_label", "add_label_id", "label_id"),
    "remove_work_item_label": _workitem("manage_label", "remove_label_id", "label_id"),
    "add_work_items_to_cycle": _container("cycle", "cycle_id", "add_ids", "work_item_ids", many=True),
    "remove_work_item_from_cycle": _container("cycle", "cycle_id", "remove_ids", "work_item_id", many=False),
    "add_work_items_to_module": _container("module", "module_id", "add_ids", "work_item_ids", many=True),
    "remove_work_item_from_module": _container("module", "module_id", "remove_ids", "work_item_id", many=False),
}


def _joined_ids(old_arg: str):
    """Hand a list of ids to a resource argument that takes them as one JSON string.

    A bare string is one id, not a sequence of characters.
    """

    async def join(**kwargs: Any):
        value = kwargs[old_arg]
        ids = [value] if isinstance(value, str) else list(value)
        return await forward(**{**kwargs, old_arg: json.dumps(ids)})

    return join


def _renamed(old_arg: str, alias: Alias) -> ArgTransform:
    if old_arg == alias.id_list:
        return ArgTransform(name=old_arg, required=True, type=list[str])
    return ArgTransform(name=old_arg, required=True)


def _argument_transforms(parent: Tool, alias: Alias) -> dict[str, ArgTransform]:
    properties = (parent.parameters or {}).get("properties", {})
    hidden = {name: ArgTransform(hide=True) for name in properties if name not in alias.exposed and name != "action"}
    renamed = {parent_arg: _renamed(old_arg, alias) for parent_arg, old_arg in alias.renames.items()}
    kept = {name: ArgTransform(required=True) for name in alias.keeps}
    return {"action": ArgTransform(hide=True, default=alias.action), **hidden, **kept, **renamed}


def alias_tool(parent: Tool, name: str, alias: Alias) -> Tool:
    """The old tool ``name``, served by ``parent`` with its action pre-bound."""
    return Tool.from_tool(
        parent,
        name=name,
        transform_fn=_joined_ids(alias.id_list) if alias.id_list else None,
        transform_args=_argument_transforms(parent, alias),
    )


class MonetaAliases(Transform):
    async def get_tool(self, name: str, call_next: GetToolNext, *, version=None) -> Tool | None:
        alias = ALIASES.get(name)
        if alias is None:
            return await call_next(name, version=version)
        logger.info("Plane MCP: fork tool name %r resolved to %r %r", name, alias.resource, alias.action)
        parent = await call_next(alias.resource, version=version)
        return alias_tool(parent, name, alias) if parent is not None else None
