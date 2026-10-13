"""Moneta fork — the eight add/remove tool names the curated list carried before v0.3.4.

Each resolves to its ``manage_*`` action with the argument names it shipped with
(as defined on ``origin/foss-sandbox`` before the sync), on ``tools/call`` only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from plane_mcp.moneta.aliases import ALIASES
from plane_mcp.server import get_stdio_mcp

PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
LABEL = "44444444-4444-4444-4444-444444444444"
OTHER = "66666666-6666-6666-6666-666666666666"

# name -> (arguments the old tool took, the resource tool, the arguments it receives)
CALLS: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {
    "add_work_item_assignee": (
        {"project_id": PROJECT, "work_item_id": ITEM, "user_id": USER},
        "workitem",
        {"action": "manage_assignee", "workitem_id": ITEM, "add_user_id": USER, "remove_user_id": ""},
    ),
    "remove_work_item_assignee": (
        {"project_id": PROJECT, "work_item_id": ITEM, "user_id": USER},
        "workitem",
        {"action": "manage_assignee", "workitem_id": ITEM, "add_user_id": "", "remove_user_id": USER},
    ),
    "add_work_item_label": (
        {"project_id": PROJECT, "work_item_id": ITEM, "label_id": LABEL},
        "workitem",
        {"action": "manage_label", "workitem_id": ITEM, "add_label_id": LABEL, "remove_label_id": ""},
    ),
    "remove_work_item_label": (
        {"project_id": PROJECT, "work_item_id": ITEM, "label_id": LABEL},
        "workitem",
        {"action": "manage_label", "workitem_id": ITEM, "add_label_id": "", "remove_label_id": LABEL},
    ),
    "add_work_items_to_cycle": (
        {"project_id": PROJECT, "cycle_id": "c", "work_item_ids": [ITEM, OTHER]},
        "cycle",
        {"action": "manage_workitems", "cycle_id": "c", "add_ids": json.dumps([ITEM, OTHER]), "remove_ids": ""},
    ),
    "remove_work_item_from_cycle": (
        {"project_id": PROJECT, "cycle_id": "c", "work_item_id": ITEM},
        "cycle",
        {"action": "manage_workitems", "cycle_id": "c", "add_ids": "", "remove_ids": ITEM},
    ),
    "add_work_items_to_module": (
        {"project_id": PROJECT, "module_id": "m", "work_item_ids": [ITEM, OTHER]},
        "module",
        {"action": "manage_workitems", "module_id": "m", "add_ids": json.dumps([ITEM, OTHER]), "remove_ids": ""},
    ),
    "remove_work_item_from_module": (
        {"project_id": PROJECT, "module_id": "m", "work_item_id": ITEM},
        "module",
        {"action": "manage_workitems", "module_id": "m", "add_ids": "", "remove_ids": ITEM},
    ),
}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


def run(coro):
    return asyncio.run(coro)


def test_every_alias_is_covered():
    assert set(CALLS) == set(ALIASES)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_alias_is_not_listed(server, name):
    async def listed():
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    assert name not in run(listed())


@pytest.mark.parametrize("name", sorted(CALLS))
def test_alias_keeps_its_original_argument_names(server, name):
    old_arguments, _, _ = CALLS[name]
    tool = run(server.get_tool(name))
    properties = set(tool.parameters["properties"])
    assert properties == set(old_arguments) | {"workspace_slug"}
    assert set(tool.parameters["required"]) == set(old_arguments)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_alias_reaches_its_manage_action(server, monkeypatch, name):
    old_arguments, resource, expected = CALLS[name]
    received: dict[str, Any] = {}
    parent = run(server.get_tool(resource))
    original_run = type(parent).run

    async def recording_run(self, arguments, *args, **kwargs):
        if self.name == resource:
            received.update(arguments)
            raise ToolError(f"reached {resource}")
        return await original_run(self, arguments, *args, **kwargs)

    monkeypatch.setattr(type(parent), "run", recording_run)

    async def call():
        async with Client(server) as c:
            await c.call_tool(name, old_arguments)

    with pytest.raises(ToolError, match=f"reached {resource}"):
        run(call())
    for key, value in expected.items():
        assert (received.get(key) or "") == value, key
    assert received["project_id"] == PROJECT


def test_alias_is_still_subject_to_visibility(monkeypatch):
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "workitem_comment")
    server = get_stdio_mcp()

    async def call():
        async with Client(server) as c:
            await c.call_tool("add_work_item_label", {"project_id": PROJECT, "work_item_id": ITEM, "label_id": LABEL})

    with pytest.raises(ToolError, match="Unknown tool"):
        run(call())


def test_alias_refused_when_its_action_is_not_curated(monkeypatch):
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "list_work_items")
    server = get_stdio_mcp()

    async def call():
        async with Client(server) as c:
            await c.call_tool("add_work_item_label", {"project_id": PROJECT, "work_item_id": ITEM, "label_id": LABEL})

    with pytest.raises(ToolError, match="not available on workitem"):
        run(call())


@pytest.mark.parametrize(
    ("value", "expected"),
    [([ITEM, OTHER], [ITEM, OTHER]), (ITEM, [ITEM]), ((ITEM,), [ITEM])],
    ids=["list", "bare-string", "tuple"],
)
def test_joined_ids_wraps_a_bare_string_without_the_coercion_middleware(monkeypatch, value, expected):
    """Called directly, so nothing upstream of the transform has repaired the argument."""
    from plane_mcp.moneta import aliases

    forwarded: dict[str, Any] = {}

    async def fake_forward(**kwargs: Any) -> None:
        forwarded.update(kwargs)

    monkeypatch.setattr(aliases, "forward", fake_forward)

    run(aliases._joined_ids("work_item_ids")(work_item_ids=value, project_id=PROJECT))

    assert json.loads(forwarded["work_item_ids"]) == expected
    assert forwarded["project_id"] == PROJECT
