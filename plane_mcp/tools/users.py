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

        The Plane ``GET /users/me`` call is not workspace-scoped, but this MCP tool still uses
        ``get_plane_client_context``, which requires a **resolved workspace slug** for the session:
        stdio ``PLANE_WORKSPACE_SLUG``, PAT or Cognito headers / claims, the ``workspace_slug`` argument
        here, or ``PLANE_WORKSPACE_SLUG`` in the server environment. For browser OAuth against Cognito,
        if the session has no workspace yet, use ``list_workspaces`` (or set ``PLANE_WORKSPACE_SLUG``)
        before calling ``get_me``.

        Args:
            workspace_slug: Optional; overrides default workspace for this request context.

        Returns:
            UserLite object containing current user information
        """
        client, _workspace_slug = get_plane_client_context(workspace_slug_from_client=workspace_slug)
        return client.users.get_me()
