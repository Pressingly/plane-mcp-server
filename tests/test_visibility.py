"""Tool visibility tests for the Moneta fork.

The real server exposes exactly the curated resources on ``tools/list``, each with
exactly its curated actions. Hidden resources stay registered but are not listed,
and a call to a hidden resource or a hidden action is rejected, whether it arrives
on the resource name or on one of upstream's retired tool names.

No live Plane backend needed: visibility is resolved entirely at startup.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from plane_mcp.moneta.visibility import (
    ALWAYS_ENABLED,
    ENABLED_ACTIONS,
    _registered_tools,
    catalogue,
    keep_action_lines,
    parse_enabled_tools,
)
from plane_mcp.server import get_stdio_mcp

EXPECTED_VISIBLE = set(ENABLED_ACTIONS) | set(ALWAYS_ENABLED)

# Representative hidden resources: Cloud-only features and the PQL reference.
HIDDEN_RESOURCES = ["milestone", "initiative", "customer", "workitem_type", "get_pql_reference"]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    """The real server as a transport builds it, with the env override cleared."""
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


async def listed_tools(mcp: FastMCP) -> dict:
    async with Client(mcp) as client:
        return {t.name: t for t in await client.list_tools()}


def test_curated_resources_and_actions_all_exist() -> None:
    """Every curated resource and action is declared upstream (catches renames)."""
    resources = catalogue()
    for resource, actions in ENABLED_ACTIONS.items():
        assert resource in resources, resource
        assert actions <= set(resources[resource]), resource


def test_hidden_resources_stay_registered(server: FastMCP) -> None:
    registered = {t.name for t in _registered_tools(server)}
    assert set(HIDDEN_RESOURCES) <= registered


@pytest.mark.anyio
async def test_tools_list_is_exactly_the_curated_set(server: FastMCP) -> None:
    assert set(await listed_tools(server)) == EXPECTED_VISIBLE


@pytest.mark.anyio
async def test_each_resource_advertises_only_its_curated_actions(server: FastMCP) -> None:
    tools = await listed_tools(server)
    for resource, actions in ENABLED_ACTIONS.items():
        advertised = tools[resource].inputSchema["properties"]["action"]["enum"]
        assert set(advertised) == actions, resource
        hidden = set(catalogue()[resource]) - actions
        described = {line.split(" (")[0].strip() for line in tools[resource].description.split("\n")}
        assert not hidden & described, resource


@pytest.mark.anyio
async def test_hidden_resource_call_is_rejected(server: FastMCP) -> None:
    async with Client(server) as client:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("milestone", {"action": "list", "project_id": "p"})


@pytest.mark.anyio
async def test_hidden_action_call_is_rejected(server: FastMCP) -> None:
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not available on workitem"):
            await client.call_tool("workitem", {"action": "delete", "project_id": "p", "workitem_id": "w"})


@pytest.mark.anyio
async def test_retired_name_cannot_reach_a_hidden_action(server: FastMCP) -> None:
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not available on workitem"):
            await client.call_tool("delete_work_item", {"project_id": "p", "work_item_id": "w"})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("list_milestones", {"project_id": "p"})


@pytest.mark.anyio
async def test_env_override_replaces_the_curated_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resource names enable every action; retired names enable the one they map to."""
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "label, delete_work_item")
    tools = await listed_tools(get_stdio_mcp())

    assert set(tools) == {"label", "workitem"} | set(ALWAYS_ENABLED)
    assert set(tools["label"].inputSchema["properties"]["action"]["enum"]) == set(catalogue()["label"])
    assert tools["workitem"].inputSchema["properties"]["action"]["enum"] == ["delete"]


@pytest.mark.anyio
async def test_list_workspaces_survives_a_bad_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An override that omits list_workspaces must not brick workspace resolution."""
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "project")
    assert set(await listed_tools(get_stdio_mcp())) == {"project", "list_workspaces"}


def test_unknown_override_entries_are_dropped() -> None:
    assert parse_enabled_tools("nope, project") == {"project": frozenset(catalogue()["project"])}
    assert parse_enabled_tools(" , ") is None


def test_keep_action_lines_repunctuates_the_last_kept_action() -> None:
    description = "Summary.\n\nActions:\n  a (x);\n  b (y);\n  c (z).\n\nFooter."
    assert keep_action_lines(description, frozenset({"a", "b"})) == (
        "Summary.\n\nActions:\n  a (x);\n  b (y).\n\nFooter."
    )


def test_list_workspaces_has_no_injected_workspace_arg(server: FastMCP) -> None:
    tool = next(t for t in _registered_tools(server) if t.name == "list_workspaces")
    assert "workspace_slug" not in (tool.parameters or {}).get("properties", {})


def test_workspace_arg_injected_into_scoped_tools(server: FastMCP) -> None:
    tool = next(t for t in _registered_tools(server) if t.name == "project")
    assert "workspace_slug" in (tool.parameters or {}).get("properties", {})


@pytest.mark.anyio
async def test_workspace_slug_passes_action_validation(server: FastMCP, monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's validator refuses undeclared args; the injected workspace_slug must get through."""
    import plane_mcp.tools.project as project_tools

    seen = []

    def context():
        from plane_mcp.moneta.workspace import resolve_workspace

        seen.append(resolve_workspace(None, ""))
        raise RuntimeError("stop before Plane")

    monkeypatch.setattr(project_tools, "get_plane_client_context", context)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="stop before Plane"):
            await client.call_tool("project", {"action": "list", "workspace_slug": "acme"})
    assert seen == ["acme"]
