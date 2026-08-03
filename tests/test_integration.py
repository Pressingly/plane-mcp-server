"""
Simple integration test for Plane MCP Server.

Environment Variables Required:
    PLANE_TEST_API_KEY: API key for authentication
    PLANE_TEST_WORKSPACE_SLUG: Workspace slug for testing
    PLANE_TEST_MCP_URL: MCP server URL (default: http://localhost:8211)
"""

import asyncio
import os
import uuid

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from plane_mcp.moneta.visibility import ALWAYS_ENABLED, ENABLED_TOOLS


def get_config():
    """Load test configuration from environment."""
    api_key = os.getenv("PLANE_TEST_API_KEY", "")
    workspace_slug = os.getenv("PLANE_TEST_WORKSPACE_SLUG", "")
    mcp_url = os.getenv("PLANE_TEST_MCP_URL", "http://localhost:8211")

    if not api_key or not workspace_slug:
        raise RuntimeError(
            "Missing required env vars: PLANE_TEST_API_KEY, PLANE_TEST_WORKSPACE_SLUG"
        )

    return {
        "api_key": api_key,
        "workspace_slug": workspace_slug,
        "mcp_url": mcp_url,
    }


def extract_result(result):
    """Extract data from MCP tool result."""
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return result.structured_content
    if hasattr(result, "content") and result.content:
        import json

        content = result.content[0]
        if hasattr(content, "text"):
            try:
                return json.loads(content.text)
            except:
                return {"raw": content.text}
    return {}


async def run_integration_test():
    """
    Full integration test:
    1. Create a project
    2. Create work item 1
    3. Create work item 2
    4. Update work item 2 with work item 1 as parent
    5. Delete work items
    6. Delete project
    """
    config = get_config()
    unique_id = uuid.uuid4().hex[:6]

    transport = StreamableHttpTransport(
        f"{config['mcp_url']}/http/api-key/mcp",
        headers={
            "x-workspace-slug": config["workspace_slug"],
            "authorization": f"Bearer {config['api_key']}",
        },
    )

    async with Client(transport=transport) as client:
        # 1. Create project
        print("Creating project...")
        project_result = await client.call_tool(
            "create_project",
            {
                "name": f"Test Project {unique_id}",
                "identifier": f"TP{unique_id[:3].upper()}",
                "description": "Integration test project",
            },
        )
        project = extract_result(project_result)
        project_id = project["id"]
        print(f"Created project: {project_id}")

        # 2. Create work item 1
        print("Creating work item 1...")
        work_item_1_result = await client.call_tool(
            "create_work_item",
            {
                "project_id": project_id,
                "name": f"Parent Work Item {unique_id}",
            },
        )
        work_item_1 = extract_result(work_item_1_result)
        work_item_1_id = work_item_1["id"]
        print(f"Created work item 1: {work_item_1_id}")

        # 3. Create work item 2
        print("Creating work item 2...")
        work_item_2_result = await client.call_tool(
            "create_work_item",
            {
                "project_id": project_id,
                "name": f"Child Work Item {unique_id}",
            },
        )
        work_item_2 = extract_result(work_item_2_result)
        work_item_2_id = work_item_2["id"]
        print(f"Created work item 2: {work_item_2_id}")

        # 4. Update work item 2 with work item 1 as parent
        print("Setting parent relationship...")
        await client.call_tool(
            "update_work_item",
            {
                "project_id": project_id,
                "work_item_id": work_item_2_id,
                "parent": work_item_1_id,
            },
        )
        print("Set work item 1 as parent of work item 2")

        # 5. Delete work items
        print("Deleting work items...")
        await client.call_tool(
            "delete_work_item",
            {"project_id": project_id, "work_item_id": work_item_2_id},
        )
        print("Deleted work item 2")

        await client.call_tool(
            "delete_work_item",
            {"project_id": project_id, "work_item_id": work_item_1_id},
        )
        print("Deleted work item 1")

        # 6. Delete project
        print("Deleting project...")
        await client.call_tool("delete_project", {"project_id": project_id})
        print("Deleted project")

        print("Integration test passed!")


# Every tool run_integration_test() calls. PLANE_MCP_ENABLED_TOOLS *replaces* the
# curated set, so all of these must be named in it — listing only the hidden ones
# would un-skip the test into a mid-run "Unknown tool" and orphan the project.
_WRITE_PATH_TOOLS = {
    "create_project",
    "create_work_item",
    "update_work_item",
    "delete_work_item",
    "delete_project",
}


def _write_path_exposed() -> bool:
    """True when the operator has opted every tool this flow calls back in."""
    override = {n.strip() for n in os.environ.get("PLANE_MCP_ENABLED_TOOLS", "").split(",") if n.strip()}
    return _WRITE_PATH_TOOLS <= override


@pytest.mark.skipif(
    not _write_path_exposed(),
    reason=f"Moneta fork: this flow's tools are registered but hidden by the whitelist. "
    f"Set PLANE_MCP_ENABLED_TOOLS to include all of: {', '.join(sorted(_WRITE_PATH_TOOLS))}.",
)
def test_full_integration():
    """Pytest entry point - runs the async integration test."""
    asyncio.run(run_integration_test())


async def run_tools_availability_test():
    """
    Test that the live server exposes exactly the Moneta whitelist.

    Moneta fork: tools/list is a static curated set, so this asserts an exact
    match rather than a subset — a stray extra tool is as much a bug as a
    missing one.
    """
    config = get_config()
    expected = set(ENABLED_TOOLS) | set(ALWAYS_ENABLED)

    transport = StreamableHttpTransport(
        f"{config['mcp_url']}/http/api-key/mcp",
        headers={
            "x-workspace-slug": config["workspace_slug"],
            "authorization": f"Bearer {config['api_key']}",
        },
    )

    async with Client(transport=transport) as client:
        # Get list of available tools
        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools}

        print(f"Found {len(tool_names)} tools on the server")

        missing = sorted(expected - tool_names)
        unexpected = sorted(tool_names - expected)
        if missing or unexpected:
            raise AssertionError(f"tools/list mismatch — missing: {missing}, unexpected: {unexpected}")

        print(f"All {len(expected)} whitelisted tools are exposed, and nothing else!")
        print("Tools availability test passed!")


def test_tools_availability():
    """Pytest entry point - verifies the live server exposes exactly the whitelist."""
    asyncio.run(run_tools_availability_test())


if __name__ == "__main__":
    asyncio.run(run_integration_test())
