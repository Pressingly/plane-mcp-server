"""Moneta fork — workspace_slug injection onto workspace-scoped tools."""

import asyncio

from fastmcp import Client, FastMCP

import plane_mcp.moneta.workspace as ws
from plane_mcp.moneta.inject import add_workspace_arg


def test_injects_optional_workspace_slug_and_propagates_per_call():
    mcp = FastMCP("t")

    @mcp.tool()
    def sample(project_id: str) -> dict:
        # Mirrors how get_plane_client_context reads the per-call override.
        return {"project_id": project_id, "resolved": ws.resolve_workspace(None, "ENVWS")}

    add_workspace_arg(mcp)

    async def run():
        async with Client(mcp) as c:
            listed = await c.list_tools()
            tool = next(t for t in listed if t.name == "sample")
            assert "workspace_slug" in tool.inputSchema["properties"]
            assert "project_id" in tool.inputSchema["properties"]  # original arg preserved
            assert "workspace_slug" not in (tool.inputSchema.get("required") or [])  # optional
            r_arg = await c.call_tool("sample", {"project_id": "P1", "workspace_slug": "acme"})
            r_env = await c.call_tool("sample", {"project_id": "P2"})
            return r_arg.data, r_env.data

    arg_data, env_data = asyncio.run(run())
    assert arg_data["resolved"] == "acme"  # per-call arg wins
    assert env_data["resolved"] == "ENVWS"  # falls back when no arg given (isolated per call)


def test_skips_list_workspaces():
    mcp = FastMCP("t")

    @mcp.tool()
    def list_workspaces() -> list:
        return []

    add_workspace_arg(mcp)

    async def run():
        async with Client(mcp) as c:
            listed = await c.list_tools()
            return next(t for t in listed if t.name == "list_workspaces")

    tool = asyncio.run(run())
    assert "workspace_slug" not in (tool.inputSchema.get("properties") or {})


def test_idempotent_when_already_injected():
    mcp = FastMCP("t")

    @mcp.tool()
    def sample(x: str) -> str:
        return x

    add_workspace_arg(mcp)
    add_workspace_arg(mcp)  # second pass must not double-add or error

    async def run():
        async with Client(mcp) as c:
            listed = await c.list_tools()
            return next(t for t in listed if t.name == "sample")

    tool = asyncio.run(run())
    props = tool.inputSchema["properties"]
    assert "workspace_slug" in props and "x" in props
