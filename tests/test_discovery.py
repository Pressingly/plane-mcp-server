"""Tests for the tool discovery and dynamic enablement module.

Verifies that enable_tools persists at the provider level (not session-scoped)
so it works correctly with stateless_http=True.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from plane_mcp.moneta.discovery import (
    DEFAULT_TOOLS,
    META_TOOLS,
    _get_all_tools,
    build_tool_catalog,
    register_discovery_tools,
)


@pytest.fixture()
def mcp_with_discovery():
    """Build a FastMCP server with a few dummy tools and discovery enabled."""
    mcp = FastMCP("test")

    @mcp.tool()
    def list_projects() -> str:
        """List projects."""
        return "projects"

    @mcp.tool()
    def create_work_item(title: str) -> str:
        """Create a work item."""
        return title

    @mcp.tool()
    def create_cycle(name: str) -> str:
        """Create a cycle."""
        return name

    @mcp.tool()
    def list_cycles() -> str:
        """List cycles."""
        return "cycles"

    @mcp.tool()
    def update_module(name: str) -> str:
        """Update a module."""
        return name

    build_tool_catalog(mcp)
    register_discovery_tools(mcp)
    return mcp


def _visible_tool_names(mcp: FastMCP) -> set[str]:
    """Return names of tools the server exposes after visibility filtering."""
    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(mcp.list_tools())
        return {t.name for t in tools}
    finally:
        loop.close()


class TestDefaultVisibility:
    """Verify that startup visibility is applied correctly."""

    def test_default_tools_are_enabled(self, mcp_with_discovery):
        visible = _visible_tool_names(mcp_with_discovery)
        for name in DEFAULT_TOOLS:
            if name in {t.name for t in _get_all_tools(mcp_with_discovery)}:
                assert name in visible, f"{name} should be enabled by default"

    def test_meta_tools_are_enabled(self, mcp_with_discovery):
        visible = _visible_tool_names(mcp_with_discovery)
        for name in META_TOOLS:
            if name in {t.name for t in _get_all_tools(mcp_with_discovery)}:
                assert name in visible, f"{name} (meta) should always be enabled"

    def test_non_default_tools_are_disabled(self, mcp_with_discovery):
        visible = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" not in visible
        assert "list_cycles" not in visible
        assert "update_module" not in visible


class TestEnableTools:
    """Verify that enable_tools mutates provider-level visibility."""

    def test_enable_makes_tool_visible(self, mcp_with_discovery):
        visible_before = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" not in visible_before

        enable_fn = None
        for t in _get_all_tools(mcp_with_discovery):
            if t.name == "enable_tools":
                enable_fn = t.fn
                break
        assert enable_fn is not None

        result = enable_fn(tool_names=["create_cycle"])
        assert "create_cycle" in result

        visible_after = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" in visible_after

    def test_enable_multiple_tools(self, mcp_with_discovery):
        enable_fn = None
        for t in _get_all_tools(mcp_with_discovery):
            if t.name == "enable_tools":
                enable_fn = t.fn
                break

        result = enable_fn(tool_names=["create_cycle", "list_cycles"])
        assert "create_cycle" in result
        assert "list_cycles" in result

        visible = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" in visible
        assert "list_cycles" in visible

    def test_enable_unknown_tool_reports_error(self, mcp_with_discovery):
        enable_fn = None
        for t in _get_all_tools(mcp_with_discovery):
            if t.name == "enable_tools":
                enable_fn = t.fn
                break

        result = enable_fn(tool_names=["nonexistent_tool"])
        assert "No valid tool names" in result
        assert "nonexistent_tool" in result

    def test_enable_preserves_previously_enabled(self, mcp_with_discovery):
        """Enabling new tools should not disable default or previously enabled ones."""
        enable_fn = None
        for t in _get_all_tools(mcp_with_discovery):
            if t.name == "enable_tools":
                enable_fn = t.fn
                break

        enable_fn(tool_names=["create_cycle"])
        enable_fn(tool_names=["list_cycles"])

        visible = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" in visible
        assert "list_cycles" in visible
        assert "list_projects" in visible

    def test_enable_is_not_session_scoped(self, mcp_with_discovery):
        """The key test: enablement must persist at provider level, not session."""
        enable_fn = None
        for t in _get_all_tools(mcp_with_discovery):
            if t.name == "enable_tools":
                enable_fn = t.fn
                break

        enable_fn(tool_names=["create_cycle"])

        # Simulate a fresh stateless request by directly querying visibility
        # (no session context — just like stateless_http=True would behave)
        visible = _visible_tool_names(mcp_with_discovery)
        assert "create_cycle" in visible, (
            "enable_tools must persist at provider level, not session level"
        )
