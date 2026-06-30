"""End-to-end MCP protocol test for tool discovery.

Simulates the exact flow an LLM client (Askii, Claude) performs:
  1. Connect via MCP protocol
  2. List tools → only default set visible
  3. Call enable_tools(["create_cycle"])
  4. List tools again → create_cycle is now visible
  5. Call list_available_tools → create_cycle shows as ENABLED

No live Plane backend needed — discovery tools are self-contained.
"""

import json

import pytest
from fastmcp import Client, FastMCP

from plane_mcp.moneta.discovery import (
    DEFAULT_TOOLS,
    META_TOOLS,
    build_tool_catalog,
    register_discovery_tools,
)


@pytest.fixture()
def discovery_server():
    """Build a FastMCP server with dummy domain tools + discovery."""
    mcp = FastMCP("test-discovery-e2e")

    @mcp.tool()
    def list_projects() -> str:
        """List all projects in the workspace."""
        return json.dumps([{"id": "p1", "name": "Test Project"}])

    @mcp.tool()
    def create_work_item(title: str) -> str:
        """Create a new work item."""
        return json.dumps({"id": "wi1", "title": title})

    @mcp.tool()
    def list_work_items() -> str:
        """List work items."""
        return json.dumps([])

    @mcp.tool()
    def update_work_item(work_item_id: str, title: str) -> str:
        """Update a work item."""
        return json.dumps({"id": work_item_id, "title": title})

    @mcp.tool()
    def search_work_items(query: str) -> str:
        """Search work items."""
        return json.dumps([])

    @mcp.tool()
    def create_cycle(name: str, project_id: str) -> str:
        """Create a new cycle in a project."""
        return json.dumps({"id": "c1", "name": name, "project_id": project_id})

    @mcp.tool()
    def list_cycles(project_id: str) -> str:
        """List all cycles in a project."""
        return json.dumps([])

    @mcp.tool()
    def list_labels() -> str:
        """List all labels."""
        return json.dumps([])

    build_tool_catalog(mcp)
    register_discovery_tools(mcp)
    return mcp


@pytest.mark.anyio
async def test_enable_tools_e2e_protocol_flow(discovery_server):
    """Full MCP protocol round-trip: list → enable → list → verify."""
    async with Client(discovery_server) as client:
        # 1. Initial tool list — only defaults + meta
        tools_before = await client.list_tools()
        names_before = {t.name for t in tools_before}

        for name in DEFAULT_TOOLS:
            assert name in names_before, f"Default tool {name} should be visible"
        # list_workspaces is a moneta-specific tool not registered in this test server
        registered_meta = META_TOOLS & {t.name for t in tools_before}
        for name in registered_meta:
            assert name in names_before, f"Meta tool {name} should be visible"

        assert "create_cycle" not in names_before, "create_cycle should be hidden initially"
        assert "list_cycles" not in names_before, "list_cycles should be hidden initially"
        assert "list_labels" not in names_before, "list_labels should be hidden initially"

        # 2. Call enable_tools via MCP protocol
        result = await client.call_tool("enable_tools", {"tool_names": ["create_cycle", "list_cycles"]})
        result_text = result.content[0].text
        assert "create_cycle" in result_text
        assert "list_cycles" in result_text

        # 3. List tools again — newly enabled tools must appear
        tools_after = await client.list_tools()
        names_after = {t.name for t in tools_after}

        assert "create_cycle" in names_after, "create_cycle should be visible after enable_tools"
        assert "list_cycles" in names_after, "list_cycles should be visible after enable_tools"

        # Default tools must still be there
        for name in DEFAULT_TOOLS:
            assert name in names_after, f"Default tool {name} must remain visible"

        # Non-enabled tools must still be hidden
        assert "list_labels" not in names_after, "list_labels should remain hidden"

        # 4. Call list_available_tools and verify status
        catalog_result = await client.call_tool("list_available_tools", {})
        catalog_text = catalog_result.content[0].text
        assert "create_cycle" in catalog_text
        assert "(ENABLED)" in catalog_text

        # 5. Actually call the newly enabled tool to prove it's callable
        cycle_result = await client.call_tool(
            "create_cycle", {"name": "Sprint 1", "project_id": "p1"}
        )
        cycle_data = json.loads(cycle_result.content[0].text)
        assert cycle_data["name"] == "Sprint 1"


@pytest.mark.anyio
async def test_enable_unknown_tool_e2e(discovery_server):
    """Enabling a nonexistent tool should report an error, not crash."""
    async with Client(discovery_server) as client:
        result = await client.call_tool("enable_tools", {"tool_names": ["bogus_tool"]})
        result_text = result.content[0].text
        assert "No valid tool names" in result_text
        assert "bogus_tool" in result_text


@pytest.mark.anyio
async def test_enable_preserves_defaults_e2e(discovery_server):
    """Enabling new tools must not hide previously-enabled defaults."""
    async with Client(discovery_server) as client:
        await client.call_tool("enable_tools", {"tool_names": ["create_cycle"]})
        await client.call_tool("enable_tools", {"tool_names": ["list_labels"]})

        tools = await client.list_tools()
        names = {t.name for t in tools}

        assert "create_cycle" in names
        assert "list_labels" in names
        assert "list_projects" in names
        assert "create_work_item" in names
