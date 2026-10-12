"""Moneta fork — PQL is rejected, not silently ignored, on the self-hosted server."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import plane_mcp.tools.cycle as cycle_tools
import plane_mcp.tools.module as module_tools
import plane_mcp.tools.workitem as workitem_tools
from plane_mcp.moneta.pql_guard import (
    PQL_TOOLS,
    PQL_UNSUPPORTED_DESCRIPTION,
    PQL_UNSUPPORTED_ERROR,
    disable_pql,
    strip_pql_mentions,
)
from plane_mcp.pql_reference import PQL_FIELD_HINT
from plane_mcp.server import get_stdio_mcp

PROJECT = "11111111-1111-1111-1111-111111111111"

# One action per PQL tool that takes pql, with the ids it requires.
PQL_CALLS = {
    "workitem": {"action": "list", "project_id": PROJECT},
    "cycle": {"action": "list_workitems", "project_id": PROJECT, "cycle_id": "c"},
    "module": {"action": "list_workitems", "project_id": PROJECT, "module_id": "m"},
}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


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
    assert PQL_TOOLS <= tools.keys()
    for name in PQL_TOOLS:
        properties = tools[name].inputSchema["properties"]
        assert properties["pql"]["description"] == PQL_UNSUPPORTED_DESCRIPTION, name
        assert "workspace_slug" in properties, name
        assert "PQL" not in tools[name].description, name
        assert "pql" not in tools[name].description, name


def test_workitem_list_no_longer_offers_the_workspace_wide_list(server):
    description = list_tools(server)["workitem"].description
    list_line = next(line for line in description.split("\n") if line.startswith("  list ("))
    assert "omit project_id" not in description
    assert list_line.startswith("  list (project_id; optional ")
    assert "project_id," not in list_line.split("optional", 1)[1]


@pytest.mark.parametrize("tool", sorted(PQL_TOOLS))
def test_pql_raises_instead_of_reaching_plane(server, monkeypatch, tool):
    def unreachable():
        raise AssertionError("pql must be rejected before a Plane client is built")

    for module in (workitem_tools, cycle_tools, module_tools):
        monkeypatch.setattr(module, "get_plane_client_context", unreachable)

    with pytest.raises(ToolError, match="PQL filtering is not supported"):
        call(server, tool, {**PQL_CALLS[tool], "pql": "assignee = currentUser()"})


def test_pql_raises_on_a_retired_name_too(server, monkeypatch):
    monkeypatch.setattr(workitem_tools, "get_plane_client_context", lambda: pytest.fail("reached the client"))

    with pytest.raises(ToolError, match="PQL filtering is not supported"):
        call(server, "list_work_items", {"project_id": PROJECT, "pql": "priority = high"})


def test_blank_pql_is_treated_as_unset(server, monkeypatch):
    seen: list[Any] = []

    def capture():
        seen.append(True)
        raise RuntimeError("reached the client")

    monkeypatch.setattr(workitem_tools, "get_plane_client_context", capture)

    with pytest.raises(ToolError, match="reached the client"):
        call(server, "workitem", {"action": "list", "project_id": PROJECT, "pql": "   "})
    assert seen == [True]


def test_error_tells_the_model_how_to_proceed():
    assert "without pql" in PQL_UNSUPPORTED_ERROR


def test_strip_pql_mentions_keeps_the_rest_of_the_description():
    description = (
        "Cycles in a project.\n\nActions:\n"
        "  list_workitems (project_id, cycle_id; optional pql, order_by);\n"
        "  count (no required params; optional project_id, pql);\n"
        "  archive (project_id, cycle_id; optional pql).\n\n"
        f"Dates are ISO 8601. {PQL_FIELD_HINT}\n"
        "These are grouping keys only -- they are not PQL filter fields."
    )
    assert strip_pql_mentions(description) == (
        "Cycles in a project.\n\nActions:\n"
        "  list_workitems (project_id, cycle_id; optional order_by);\n"
        "  count (no required params; optional project_id);\n"
        "  archive (project_id, cycle_id).\n\n"
        "Dates are ISO 8601."
    )


def test_pql_reference_is_hidden_by_default(server):
    assert "get_pql_reference" not in list_tools(server)


def test_disable_pql_is_idempotent():
    mcp = FastMCP("t")

    @mcp.tool(name="workitem")
    def workitem(project_id: str, pql: str | None = None) -> dict:
        return {"project_id": project_id}

    disable_pql(mcp)
    disable_pql(mcp)

    assert call(mcp, "workitem", {"project_id": "P"}).data == {"project_id": "P"}
    with pytest.raises(ToolError, match="PQL filtering is not supported"):
        call(mcp, "workitem", {"project_id": "P", "pql": "x"})


def test_tools_without_pql_are_left_alone():
    mcp = FastMCP("t")

    @mcp.tool(name="cycle")
    def cycle(project_id: str) -> dict:
        return {"project_id": project_id}

    disable_pql(mcp)

    assert "pql" not in list_tools(mcp)["cycle"].inputSchema["properties"]
