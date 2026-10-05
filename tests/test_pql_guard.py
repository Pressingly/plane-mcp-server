"""Moneta fork — PQL is rejected, not silently ignored, on the self-hosted server."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import plane_mcp.tools.cycles as cycle_tools
import plane_mcp.tools.modules as module_tools
import plane_mcp.tools.work_items as work_item_tools
from plane_mcp.moneta.pql_guard import (
    PQL_POINTING_TOOLS,
    PQL_TOOLS,
    PQL_UNSUPPORTED_DESCRIPTION,
    PQL_UNSUPPORTED_ERROR,
    disable_pql,
    strip_pql_mentions,
)
from plane_mcp.tools import register_tools

PROJECT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", ",".join(PQL_TOOLS | PQL_POINTING_TOOLS))
    mcp = FastMCP("t")
    register_tools(mcp)
    return mcp


def list_tools(server: FastMCP) -> dict[str, Any]:
    async def run():
        async with Client(server) as c:
            return {t.name: t for t in await c.list_tools()}

    return asyncio.run(run())


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async def run():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(run())


def test_every_pql_tool_advertises_pql_as_unsupported(server):
    tools = list_tools(server)
    registered = PQL_TOOLS & tools.keys()
    assert registered, "no PQL list tool is registered"
    for name in registered:
        properties = tools[name].inputSchema["properties"]
        assert properties["pql"]["description"] == PQL_UNSUPPORTED_DESCRIPTION, name
        assert "workspace_slug" in properties, name
        assert "PQL" not in tools[name].description, name
        assert "list_workspace_work_items" not in tools[name].description, name


def test_search_work_items_no_longer_points_at_pql(server):
    description = list_tools(server)["search_work_items"].description
    assert "PQL" not in description
    assert "free-text" in description


@pytest.mark.parametrize("tool", sorted(PQL_TOOLS))
def test_pql_raises_instead_of_reaching_plane(server, monkeypatch, tool):
    def unreachable():
        raise AssertionError("pql must be rejected before a Plane client is built")

    for module in (work_item_tools, cycle_tools, module_tools):
        monkeypatch.setattr(module, "get_plane_client_context", unreachable)
    args = {"project_id": PROJECT, "cycle_id": "c", "module_id": "m", "pql": "assignee = currentUser()"}
    schema = list_tools(server)[tool].inputSchema["properties"]

    with pytest.raises(ToolError, match="PQL filtering is not supported"):
        call(server, tool, {k: v for k, v in args.items() if k in schema})


def test_blank_pql_is_treated_as_unset(server, monkeypatch):
    seen: list[Any] = []

    def capture():
        seen.append(True)
        raise RuntimeError("reached the client")

    monkeypatch.setattr(work_item_tools, "get_plane_client_context", capture)

    with pytest.raises(ToolError, match="reached the client"):
        call(server, "list_work_items", {"project_id": PROJECT, "pql": "   "})
    assert seen == [True]


def test_error_tells_the_model_how_to_proceed_without_naming_a_dead_tool():
    assert "without pql" in PQL_UNSUPPORTED_ERROR
    assert "list_workspace_work_items" not in PQL_UNSUPPORTED_ERROR


def test_strip_pql_mentions_keeps_the_rest_of_the_description():
    description = (
        "List work items in a cycle with optional PQL filtering.\n"
        "For workspace-wide filtering use list_workspace_work_items instead.\n\n"
        "Paginated by cursor."
    )
    assert strip_pql_mentions(description) == "List work items in a cycle.\n\nPaginated by cursor."


@pytest.mark.parametrize("tool", ["get_pql_reference", "list_workspace_work_items"])
def test_tools_the_server_cannot_serve_are_hidden_by_default(monkeypatch, tool):
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    mcp = FastMCP("t")
    register_tools(mcp)
    assert tool not in list_tools(mcp)


def test_disable_pql_is_idempotent(monkeypatch):
    mcp = FastMCP("t")

    @mcp.tool()
    def list_work_items(project_id: str, pql: str | None = None) -> dict:
        return {"project_id": project_id}

    disable_pql(mcp)
    disable_pql(mcp)

    assert call(mcp, "list_work_items", {"project_id": "P"}).data == {"project_id": "P"}
    with pytest.raises(ToolError, match="PQL filtering is not supported"):
        call(mcp, "list_work_items", {"project_id": "P", "pql": "x"})


def test_tools_without_pql_are_left_alone():
    mcp = FastMCP("t")

    @mcp.tool()
    def list_cycles(project_id: str) -> dict:
        return {"project_id": project_id}

    disable_pql(mcp)

    assert "pql" not in list_tools(mcp)["list_cycles"].inputSchema["properties"]
