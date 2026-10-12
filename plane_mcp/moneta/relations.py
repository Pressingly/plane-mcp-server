"""Restore ``relates_to`` and ``duplicate`` on ``workitem_relation`` (Moneta fork).

Upstream v0.3.4 split relations into Plane Cloud's six directional dependency
types and workspace-defined custom relations, and ``create`` refuses any other
``relation_type``. The self-hosted ``/api/v1`` has neither system: its single
``relations/`` route (``IssueRelationCreateSerializer.RELATION_TYPE_CHOICES`` in
``plane/apps/api/plane/api/serializers/issue.py``) accepts the six dependency
types plus ``relates_to`` and ``duplicate``, which plane-sdk's
``WorkItemRelationTypeEnum`` spells the same way.

:func:`restore_relation_types` wraps the tool so a ``create`` naming one of the two
extra types goes straight to ``relations/``, while everything else, including the
upstream argument checks, runs unchanged. Listing needs nothing here:
:class:`plane_mcp.moneta.selfhosted.SelfHostedDependencies` already returns every
group ``relations/`` sends. The ``relation_type`` enum and the footer advertise
the full set, and the footer no longer points at custom definitions, which this
server does not have.
"""

from __future__ import annotations

from typing import Any, get_args

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger
from plane.models.enums import WorkItemRelationTypeEnum
from plane.models.work_items import CreateWorkItemRelation

from plane_mcp import client as plane_client
from plane_mcp.toolkit import coerce_list
from plane_mcp.tools.workitem_relation import DEPENDENCY_TYPES
from plane_mcp.tools.workitem_relation import FOOTER as UPSTREAM_FOOTER
from plane_mcp.tools.workitem_relation import NAME as RELATION_TOOL

logger = get_logger(__name__)

RELATION_TYPES: tuple[str, ...] = get_args(WorkItemRelationTypeEnum)
RESTORED_TYPES: frozenset[str] = frozenset(RELATION_TYPES) - frozenset(DEPENDENCY_TYPES)

SELF_HOSTED_FOOTER = (
    f"relation_type is one of: {', '.join(RELATION_TYPES)}. relates_to and duplicate are "
    "symmetric; the others set direction from this work item's side. Custom relation "
    "definitions are not available on this server."
)


def _creates_restored_type(arguments: dict[str, Any]) -> bool:
    return (
        arguments.get("action") == "create"
        and arguments.get("relation_type") in RESTORED_TYPES
        and bool(arguments.get("project_id"))
        and bool(arguments.get("workitem_id"))
        and bool(coerce_list(arguments.get("workitem_ids")))
    )


def _create_relation(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    client, workspace_slug = plane_client.get_plane_client_context()
    targets = coerce_list(arguments["workitem_ids"]) or []
    relation_type = arguments["relation_type"]
    client.work_items.relations.create(
        workspace_slug=workspace_slug,
        project_id=arguments["project_id"],
        work_item_id=arguments["workitem_id"],
        data=CreateWorkItemRelation(relation_type=relation_type, issues=targets),
    )
    return [{"id": target, "project_id": arguments["project_id"], "relation_type": relation_type} for target in targets]


async def _route_restored_types(**kwargs: Any):
    if _creates_restored_type(kwargs):
        return _create_relation(kwargs)
    return await forward(**kwargs)


def with_relation_type_enum(parameters: dict[str, Any]) -> dict[str, Any]:
    """A copy of the input schema whose ``relation_type`` lists every type this server accepts."""
    properties = parameters.get("properties", {})
    relation_type = {key: value for key, value in properties.get("relation_type", {}).items() if key != "default"}
    advertised = {**relation_type, "type": "string", "enum": list(RELATION_TYPES)}
    return {**parameters, "properties": {**properties, "relation_type": advertised}}


CUSTOM_RELATION_NOTE = " -- pass relation_type for a dependency, or definition id + label for a custom relation"
SELF_HOSTED_CREATE_NOTE = " -- pass relation_type"


def _restored(tool: Tool) -> Tool:
    description = (
        (tool.description or "")
        .replace(UPSTREAM_FOOTER, SELF_HOSTED_FOOTER)
        .replace(CUSTOM_RELATION_NOTE, SELF_HOSTED_CREATE_NOTE)
    )
    wrapped = Tool.from_tool(tool, transform_fn=_route_restored_types, description=description)
    return wrapped.model_copy(update={"parameters": with_relation_type_enum(wrapped.parameters)})


def _is_unrestored(tool: Tool) -> bool:
    enum = (tool.parameters or {}).get("properties", {}).get("relation_type", {}).get("enum") or ()
    return tool.name == RELATION_TOOL and not RESTORED_TYPES <= set(enum)


def restore_relation_types(mcp: FastMCP) -> None:
    """Let ``workitem_relation create`` take ``relates_to`` and ``duplicate`` again."""
    try:
        tools = [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]
    except AttributeError as exc:  # pragma: no cover - guards against fastmcp internals drift
        logger.warning("restore_relation_types: cannot access the tool registry (%s)", exc)
        return

    for tool in [_restored(tool) for tool in tools if _is_unrestored(tool)]:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(tool)
