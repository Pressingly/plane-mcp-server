"""Moneta fork — PQL is rejected, not silently ignored, on the self-hosted server."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import plane_mcp.tools.work_items as work_item_tools
from plane_mcp.moneta.pql_guard import (
    PQL_TOOLS,
    PQL_UNSUPPORTED_DESCRIPTION,
    PQL_UNSUPPORTED_ERROR,
    PQL_UNSUPPORTED_NOTE,
    disable_pql,
)
from plane_mcp.tools import register_tools

PROJECT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", ",".join(PQL_TOOLS | {"get_pql_reference"}))
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
        assert tools[name].description.endswith(PQL_UNSUPPORTED_NOTE), name


@pytest.mark.parametrize("tool", sorted(PQL_TOOLS))
def test_pql_raises_instead_of_reaching_plane(server, monkeypatch, tool):
    def unreachable():
        raise AssertionError("pql must be rejected before a Plane client is built")

    monkeypatch.setattr(work_item_tools, "get_plane_client_context", unreachable)
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


def test_error_tells_the_model_how_to_proceed():
    assert "without pql" in PQL_UNSUPPORTED_ERROR


def test_get_pql_reference_is_hidden_by_default(monkeypatch):
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    mcp = FastMCP("t")
    register_tools(mcp)
    assert "get_pql_reference" not in list_tools(mcp)


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
