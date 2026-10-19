"""Both MCP hints on every tool, fail-safe, and never a read for a tool that writes."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from plane_mcp.moneta.aliases import ALIASES
from plane_mcp.moneta.annotations import READ_NAMES, Effect, combined, effect_of, hints
from plane_mcp.moneta.visibility import ENABLED_ACTIONS, _registered_tools
from plane_mcp.server import get_header_mcp, get_stdio_mcp
from plane_mcp.tools.registry import RESOURCES, alias_table

READ = (True, False)
ADD = (False, False)
DESTRUCTIVE = (False, True)

GATEWAY_TOOL_NAMES = {
    "add_work_item_assignee": ADD,
    "add_work_item_label": ADD,
    "add_work_items_to_cycle": ADD,
    "add_work_items_to_module": ADD,
    "complete_cycle": DESTRUCTIVE,
    "create_cycle": ADD,
    "create_work_item": ADD,
    "create_work_item_comment": ADD,
    "create_work_item_relation": ADD,
    "delete_work_item_comment": DESTRUCTIVE,
    "get_me": READ,
    "get_project_members": READ,
    "get_workspace_members": READ,
    "list_cycle_work_items": READ,
    "list_cycles": READ,
    "list_labels": READ,
    "list_module_work_items": READ,
    "list_modules": READ,
    "list_projects": READ,
    "list_states": READ,
    "list_work_item_comments": READ,
    "list_work_item_links": READ,
    "list_work_item_relations": READ,
    "list_work_items": READ,
    "list_workspaces": READ,
    "remove_work_item_assignee": DESTRUCTIVE,
    "remove_work_item_from_cycle": DESTRUCTIVE,
    "remove_work_item_from_module": DESTRUCTIVE,
    "remove_work_item_label": DESTRUCTIVE,
    "remove_work_item_relation": DESTRUCTIVE,
    "retrieve_cycle": READ,
    "retrieve_label": READ,
    "retrieve_module": READ,
    "retrieve_project": READ,
    "retrieve_state": READ,
    "retrieve_work_item": READ,
    "retrieve_work_item_by_identifier": READ,
    "retrieve_work_item_comment": READ,
    "retrieve_work_item_link": READ,
    "search_work_items": READ,
    "transfer_cycle_work_items": DESTRUCTIVE,
    "update_cycle": DESTRUCTIVE,
    "update_project": DESTRUCTIVE,
    "update_work_item": DESTRUCTIVE,
    "update_work_item_comment": DESTRUCTIVE,
}


def pair(effect: Effect) -> tuple[bool, bool]:
    published = hints(effect)
    return published["readOnlyHint"], published["destructiveHint"]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


def hint_pair(tool) -> tuple[bool | None, bool | None]:
    annotations = tool.annotations
    return (annotations.readOnlyHint, annotations.destructiveHint) if annotations else (None, None)


@pytest.mark.parametrize(("name", "expected"), sorted(GATEWAY_TOOL_NAMES.items()))
def test_gateway_tool_names_classify(name: str, expected: tuple[bool, bool]) -> None:
    assert pair(effect_of(name)) == expected


def test_twenty_five_of_the_forty_five_listed_names_read() -> None:
    assert len(GATEWAY_TOOL_NAMES) == 45
    assert sum(effect_of(name) is Effect.READ for name in GATEWAY_TOOL_NAMES) == 25


@pytest.mark.parametrize("name", ["frobnicate", "sync_everything", "purge", "", "x_get_y", "getter", "lists"])
def test_unknown_verbs_are_destructive_writes(name: str) -> None:
    assert pair(effect_of(name)) == DESTRUCTIVE


@pytest.mark.parametrize(
    "name", ["delete_label", "remove_x", "archive", "complete", "transfer_workitems", "manage_label"]
)
def test_removing_verbs_are_destructive(name: str) -> None:
    assert pair(effect_of(name)) == DESTRUCTIVE


@pytest.mark.parametrize("name", ["create", "add_pages", "attach_to_workitem", "create_points"])
def test_adding_verbs_are_not_destructive(name: str) -> None:
    assert pair(effect_of(name)) == ADD


def test_nothing_to_judge_is_destructive() -> None:
    assert combined([]) is Effect.DESTRUCTIVE


def test_most_severe_effect_wins() -> None:
    assert combined([Effect.READ, Effect.ADD]) is Effect.ADD
    assert combined([Effect.READ, Effect.ADD, Effect.DESTRUCTIVE]) is Effect.DESTRUCTIVE


def test_no_write_action_is_ever_classified_as_a_read() -> None:
    offenders = [
        (mod.NAME, action.name)
        for mod in RESOURCES
        for action in mod.ACTIONS
        if effect_of(action.name) is Effect.READ and not action.read
    ]
    assert offenders == []


def test_every_declared_read_action_stays_a_read_or_is_flagged() -> None:
    declared_reads = {action.name for mod in RESOURCES for action in mod.ACTIONS if action.read}
    unrecognised = {name for name in declared_reads if effect_of(name) is not Effect.READ}
    assert unrecognised == set()


def test_read_names_are_all_declared_reads() -> None:
    declared_reads = {action.name for mod in RESOURCES for action in mod.ACTIONS if action.read}
    assert READ_NAMES <= declared_reads


def test_declared_destructive_actions_are_never_add_or_read() -> None:
    offenders = [
        (mod.NAME, action.name)
        for mod in RESOURCES
        for action in mod.ACTIONS
        if action.destructive and effect_of(action.name) is not Effect.DESTRUCTIVE
    ]
    assert offenders == []


def test_every_registered_tool_has_both_hints_hidden_included(server: FastMCP) -> None:
    registered = _registered_tools(server)
    hidden = {tool.name for tool in registered} - set(ENABLED_ACTIONS) - {"list_workspaces"}
    assert hidden
    assert all(None not in hint_pair(tool) for tool in registered)


def test_a_registered_tool_that_can_write_is_never_read_only(server: FastMCP) -> None:
    declared = {mod.NAME: mod.ACTIONS for mod in RESOURCES}
    for tool in _registered_tools(server):
        reachable = ENABLED_ACTIONS.get(tool.name)
        actions = [a for a in declared.get(tool.name, ()) if reachable is None or a.name in reachable]
        if any(not action.read for action in actions):
            assert tool.annotations.readOnlyHint is False, tool.name


def test_hidden_tools_cover_every_declared_action(server: FastMCP) -> None:
    by_name = {tool.name: tool for tool in _registered_tools(server)}
    assert hint_pair(by_name["milestone"]) == DESTRUCTIVE
    assert hint_pair(by_name["get_pql_reference"]) == READ
    assert hint_pair(by_name["workitem_activity"]) == READ


@pytest.mark.anyio
async def test_tools_list_publishes_both_hints(server: FastMCP) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    assert tools
    assert all(None not in hint_pair(tool) for tool in tools)


@pytest.mark.anyio
async def test_listed_classification_follows_the_curated_actions(server: FastMCP) -> None:
    async with Client(server) as client:
        listed = {tool.name: hint_pair(tool) for tool in await client.list_tools()}
    assert listed == {
        "list_workspaces": READ,
        "member": READ,
        "label": READ,
        "state": READ,
        "workitem_link": READ,
        "workitem_relation": ADD,
        "project": DESTRUCTIVE,
        "workitem": DESTRUCTIVE,
        "workitem_comment": DESTRUCTIVE,
        "cycle": DESTRUCTIVE,
        "module": DESTRUCTIVE,
    }


@pytest.mark.anyio
async def test_other_annotations_survive(server: FastMCP) -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["label"].annotations.title == "Labels"
    assert tools["label"].annotations.openWorldHint is False


@pytest.mark.anyio
async def test_enabling_a_hidden_tool_later_lists_it_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "milestone,workitem_activity,delete_work_item")
    async with Client(get_stdio_mcp()) as client:
        tools = {tool.name: hint_pair(tool) for tool in await client.list_tools()}
    assert tools["milestone"] == DESTRUCTIVE
    assert tools["workitem_activity"] == READ
    assert tools["workitem"] == DESTRUCTIVE


def severity(tool) -> Effect:
    read_only, destructive = hint_pair(tool)
    if read_only:
        return Effect.READ
    return Effect.DESTRUCTIVE if destructive else Effect.ADD


@pytest.mark.anyio
async def test_retired_names_are_never_less_severe_than_the_action_they_bind(server: FastMCP) -> None:
    bound = {**alias_table(), **{name: (alias.resource, alias.action) for name, alias in ALIASES.items()}}
    for name, (resource, action) in bound.items():
        tool = await server.get_tool(name)
        if tool is not None and action in ENABLED_ACTIONS.get(resource, frozenset()):
            assert severity(tool) >= effect_of(action), name


def test_the_header_transport_publishes_both_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    assert all(None not in hint_pair(tool) for tool in _registered_tools(get_header_mcp()))
