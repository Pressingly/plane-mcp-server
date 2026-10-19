"""Publish ``readOnlyHint`` and ``destructiveHint`` on every registered tool (Moneta fork).

A gateway deciding which tools a client may call reads these two hints, so they have
to be right and they have to fail safe:

* a tool that only reads is ``readOnlyHint=True, destructiveHint=False``;
* a tool that can only add something new is ``readOnlyHint=False, destructiveHint=False``;
* everything else, including any name this module has never seen, is
  ``readOnlyHint=False, destructiveHint=True``.

The verb is the first word of a name. ``get``, ``list``, ``retrieve`` and ``search`` read;
``create``, ``add`` and ``attach`` only add; any other verb is destructive. Upstream names
a few reads without a read verb (``me``, ``count``); :data:`READ_NAMES` lists them, and a
test pins each against the action's own ``read`` flag so a write can never be listed there.

A resource tool dispatches on ``action``, so it takes the most severe classification of
the actions it accepts: the curated ones when the visibility set names the tool, every
declared action otherwise. That covers hidden tools too, so listing one later needs no
second change. Applied from :func:`plane_mcp.moneta.surface.apply_moneta_surface`, ahead
of the visibility step.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import IntEnum

from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from mcp.types import ToolAnnotations

from plane_mcp.moneta.visibility import _registered_tools, enabled_actions

READ_VERBS = frozenset({"get", "list", "retrieve", "search"})
ADD_VERBS = frozenset({"create", "add", "attach"})
READ_NAMES = frozenset({"me", "read", "count", "worklog_summary", "download_url"})


class Effect(IntEnum):
    READ = 0
    ADD = 1
    DESTRUCTIVE = 2


def effect_of(name: str) -> Effect:
    """The effect of one tool name or action name; unknown verbs are destructive."""
    verb = name.split("_", 1)[0]
    if name in READ_NAMES or verb in READ_VERBS:
        return Effect.READ
    if verb in ADD_VERBS:
        return Effect.ADD
    return Effect.DESTRUCTIVE


def combined(effects: Iterable[Effect]) -> Effect:
    """The most severe of ``effects``; nothing to judge is destructive."""
    return max(effects, default=Effect.DESTRUCTIVE)


def hints(effect: Effect) -> dict[str, bool]:
    return {"readOnlyHint": effect is Effect.READ, "destructiveHint": effect is Effect.DESTRUCTIVE}


def declared_actions(tool: Tool) -> tuple[str, ...] | None:
    """The actions a resource tool dispatches on, or None for a tool with no ``action``."""
    action = (tool.parameters or {}).get("properties", {}).get("action")
    return None if action is None else tuple(action.get("enum") or ())


def tool_effect(tool: Tool, enabled: Mapping[str, frozenset[str]]) -> Effect:
    actions = declared_actions(tool)
    if actions is None:
        return effect_of(tool.name)
    reachable = [name for name in actions if name in enabled.get(tool.name, frozenset(actions))]
    return combined(effect_of(name) for name in reachable)


def annotated(tool: Tool, enabled: Mapping[str, frozenset[str]]) -> Tool:
    """A copy of ``tool`` with both hints set, keeping every other annotation it carries."""
    current = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
    annotations = ToolAnnotations(**{**current, **hints(tool_effect(tool, enabled))})
    return tool.model_copy(update={"annotations": annotations})


def annotate_tools(mcp: FastMCP) -> None:
    """Set both hints on every registered tool, hidden ones included."""
    enabled = enabled_actions()
    tools = _registered_tools(mcp)
    for tool in tools:
        mcp._local_provider.remove_tool(tool.name)
        mcp.add_tool(annotated(tool, enabled))
