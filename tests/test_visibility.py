"""Tool visibility tests for the Moneta fork.

Verifies the standard-MCPO behaviour: the real server exposes exactly the
curated whitelist on ``tools/list``, hidden tools are registered but not
listed, and ``tools/call`` on a hidden tool is rejected (there is no
``execute_tool`` bypass any more).

No live Plane backend needed — visibility is resolved entirely at startup.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from plane_mcp.moneta.visibility import ALWAYS_ENABLED, ENABLED_TOOLS, _get_all_tools
from plane_mcp.tools import register_tools

EXPECTED_VISIBLE = set(ENABLED_TOOLS) | set(ALWAYS_ENABLED)

# Representative hidden tools from each bucket the whitelist suppresses.
HIDDEN_SAMPLES = ["create_project", "delete_work_item", "create_state", "list_initiatives"]

# Milestones are unsupported by the Plane community edition and are never registered.
MILESTONE_TOOLS = [
    "add_work_items_to_milestone",
    "create_milestone",
    "delete_milestone",
    "list_milestone_work_items",
    "list_milestones",
    "remove_work_items_from_milestone",
    "retrieve_milestone",
    "update_milestone",
]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    """The real tool registry, with the env override cleared."""
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    mcp = FastMCP("test-visibility")
    register_tools(mcp)
    return mcp


def test_whitelist_names_all_exist(server: FastMCP) -> None:
    """Every whitelisted name matches a registered tool (catches typos/renames)."""
    registered = {t.name for t in _get_all_tools(server)}
    assert EXPECTED_VISIBLE - registered == set()


def test_milestone_tools_are_not_registered(server: FastMCP) -> None:
    registered = {t.name for t in _get_all_tools(server)}
    assert registered.isdisjoint(MILESTONE_TOOLS)


def test_hidden_tools_stay_registered(server: FastMCP) -> None:
    """Disabled tools remain in the registry so they can be re-activated later."""
    registered = {t.name for t in _get_all_tools(server)}
    for name in HIDDEN_SAMPLES:
        assert name in registered


@pytest.mark.anyio
async def test_tools_list_is_exactly_the_whitelist(server: FastMCP) -> None:
    async with Client(server) as client:
        listed = {t.name for t in await client.list_tools()}
    assert listed == EXPECTED_VISIBLE


@pytest.mark.anyio
async def test_hidden_tool_call_is_rejected(server: FastMCP) -> None:
    """No execute_tool bypass: a hidden tool cannot be invoked by name."""
    async with Client(server) as client:
        # Match the message: any exposed tool also raises ToolError here (no
        # workspace resolves), so a bare raises() would pass even if exposed.
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("create_project", {"name": "x", "identifier": "XXX"})


@pytest.mark.anyio
async def test_meta_tools_are_gone(server: FastMCP) -> None:
    """The removed discovery layer is not registered or listed anywhere."""
    registered = {t.name for t in _get_all_tools(server)}
    async with Client(server) as client:
        listed = {t.name for t in await client.list_tools()}
    for name in ("list_available_tools", "enable_tools", "execute_tool"):
        assert name not in registered
        assert name not in listed


@pytest.mark.anyio
async def test_env_override_replaces_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLANE_MCP_ENABLED_TOOLS replaces the curated set but keeps list_workspaces."""
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "list_projects, create_state")
    mcp = FastMCP("test-visibility-env")
    register_tools(mcp)

    async with Client(mcp) as client:
        listed = {t.name for t in await client.list_tools()}

    assert listed == {"list_projects", "create_state"} | set(ALWAYS_ENABLED)


@pytest.mark.anyio
async def test_list_workspaces_survives_a_bad_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An override that omits list_workspaces must not brick workspace resolution."""
    monkeypatch.setenv("PLANE_MCP_ENABLED_TOOLS", "list_projects")
    mcp = FastMCP("test-visibility-env-bad")
    register_tools(mcp)

    async with Client(mcp) as client:
        listed = {t.name for t in await client.list_tools()}

    assert listed == {"list_projects", "list_workspaces"}


def test_list_workspaces_has_no_injected_workspace_arg(server: FastMCP) -> None:
    """add_workspace_arg must keep skipping the workspace-discovery tool."""
    tool = next(t for t in _get_all_tools(server) if t.name == "list_workspaces")
    assert "workspace_slug" not in (tool.parameters or {}).get("properties", {})


def test_workspace_arg_injected_into_scoped_tools(server: FastMCP) -> None:
    tool = next(t for t in _get_all_tools(server) if t.name == "list_projects")
    assert "workspace_slug" in (tool.parameters or {}).get("properties", {})
