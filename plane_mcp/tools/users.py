"""User-related tools for Plane MCP Server."""

from fastmcp import FastMCP
from plane.models.users import UserLite

from plane_mcp.client import get_plane_client_context


def register_user_tools(mcp: FastMCP) -> None:
    """Register all user-related tools with the MCP server."""

    @mcp.tool()
    def get_me(workspace_slug: str | None = None) -> UserLite:
        """
        Get current user information.

        Args:
            workspace_slug: Optional; overrides default workspace for this request context.

        Returns:
            UserLite object containing current user information
        """
        client, _workspace_slug = get_plane_client_context(workspace_slug_from_client=workspace_slug)
        return client.users.get_me()
