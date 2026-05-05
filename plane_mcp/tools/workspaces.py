"""Workspace-related tools for Plane MCP Server."""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane.models.users import UserLite
from plane.models.workspaces import WorkspaceFeature
from pydantic import BaseModel, ConfigDict

from plane_mcp.client import _plane_bearer_for, get_plane_client_context

logger = get_logger(__name__)


class Workspace(BaseModel):
    """Workspace row from ``GET /api/users/me/workspaces/`` (shape varies; extra fields allowed)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    name: str | None = None
    slug: str | None = None
    owner: str | None = None


def _plane_root_base() -> str:
    """Return ``<plane-base>`` (no path) for non-``/api/v1`` endpoints.

    Plane's "list user workspaces" lives at ``/api/users/me/workspaces/`` (the **app** API,
    not the public ``/api/v1/`` surface — see Plane backend ``UserWorkSpacesEndpoint``).
    Prefers ``PLANE_INTERNAL_BASE_URL`` (server-to-server); otherwise ``PLANE_BASE_URL``.
    """
    base = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    return base.rstrip("/")


def _httpx_verify() -> bool | str:
    """Match the SDK's TLS trust: honor ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE``.

    httpx ignores ``REQUESTS_CA_BUNDLE`` (which the Plane SDK / requests honors), so
    when calling Traefik with a dev wildcard cert we must pass ``verify=<bundle path>``
    explicitly. Falls through to httpx's default trust store when neither is set.
    """
    bundle = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE")
    return bundle or True


def _plane_auth_headers() -> dict[str, str]:
    """Build ``Authorization`` / ``X-Api-Key`` for Plane from the current MCP session.

    Cognito browser-OAuth: ``get_access_token()`` returns the validated Cognito **access**
    token; we forward the matching **ID token** (looked up via
    ``plane_mcp.client._plane_bearer_for``) so oauth2-proxy can resolve
    ``cognito:username`` and Plane's ``ProxyAuthMiddleware`` authenticates the same
    user as the web cookie flow. PAT mount: forwards ``X-Api-Key`` instead.
    Stdio: falls back to ``PLANE_API_KEY``.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    stored: AccessToken | None = get_access_token()
    if stored:
        auth_method = (stored.claims or {}).get("auth_method", "oauth")
        if auth_method in ("api_key_env", "api_key_header"):
            headers["X-Api-Key"] = stored.token
        else:
            headers["Authorization"] = f"Bearer {_plane_bearer_for(stored.token, stored.claims)}"
        return headers
    if api_key := os.getenv("PLANE_API_KEY"):
        headers["X-Api-Key"] = api_key
    return headers


def register_workspace_tools(mcp: FastMCP) -> None:
    """Register all workspace-related tools with the MCP server."""

    @mcp.tool()
    def list_workspaces() -> list[Workspace]:
        """List Plane workspaces the authenticated user belongs to.

        Calls ``GET /api/users/me/workspaces/`` (Plane app API, not ``/api/v1/``) with the
        current bearer token (Cognito OAuth) or API key (PAT / stdio). Plane's public API
        does not list a user's workspaces — the workspace-scoped routes all require a
        slug, so this tool exists to bootstrap that slug.

        Use a returned ``slug`` to set ``PLANE_WORKSPACE_SLUG`` (or send
        ``X-Workspace-slug`` on PAT) before calling workspace-scoped tools.

        Returns:
            List of Workspace objects (id, name, slug, owner, plus any extra fields).
        """
        url = f"{_plane_root_base()}/api/users/me/workspaces/"
        with httpx.Client(timeout=30.0, verify=_httpx_verify()) as client:
            resp = client.get(url, headers=_plane_auth_headers())
            resp.raise_for_status()
            data = resp.json()
        items = data["results"] if isinstance(data, dict) and "results" in data else data
        if not isinstance(items, list):
            raise ValueError(f"Unexpected workspaces payload from {url}: {type(items).__name__}")
        return [Workspace.model_validate(item) for item in items]

    @mcp.tool()
    def get_workspace_members(workspace_slug: str | None = None) -> list[UserLite]:
        """
        Get all members of the current workspace.

        Args:
            workspace_slug: Optional; overrides default workspace for this call

        Returns:
            List of UserLite objects representing workspace members
        """
        client, workspace_slug = get_plane_client_context(workspace_slug_from_client=workspace_slug)
        return client.workspaces.get_members(workspace_slug=workspace_slug)

    @mcp.tool()
    def get_workspace_features(workspace_slug: str | None = None) -> WorkspaceFeature:
        """
        Get features of the current workspace.

        Args:
            workspace_slug: Optional; overrides default workspace for this call

        Returns:
            WorkspaceFeature object containing feature flags
        """
        client, workspace_slug = get_plane_client_context(workspace_slug_from_client=workspace_slug)
        return client.workspaces.get_features(workspace_slug=workspace_slug)

    @mcp.tool()
    def update_workspace_features(
        project_grouping: bool | None = None,
        initiatives: bool | None = None,
        teams: bool | None = None,
        customers: bool | None = None,
        wiki: bool | None = None,
        pi: bool | None = None,
        workspace_slug: str | None = None,
    ) -> WorkspaceFeature:
        """
        Update features of the current workspace.

        Args:
            project_grouping: Enable/disable project grouping feature
            initiatives: Enable/disable initiatives feature
            teams: Enable/disable teams feature
            customers: Enable/disable customers feature
            wiki: Enable/disable wiki feature
            pi: Enable/disable PI (Program Increment) feature
            workspace_slug: Optional; overrides default workspace for this call

        Returns:
            Updated WorkspaceFeature object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug_from_client=workspace_slug)

        # Build data dict with only non-None values
        feature_data: dict[str, bool] = {}
        if project_grouping is not None:
            feature_data["project_grouping"] = project_grouping
        if initiatives is not None:
            feature_data["initiatives"] = initiatives
        if teams is not None:
            feature_data["teams"] = teams
        if customers is not None:
            feature_data["customers"] = customers
        if wiki is not None:
            feature_data["wiki"] = wiki
        if pi is not None:
            feature_data["pi"] = pi

        data = WorkspaceFeature(**feature_data)

        return client.workspaces.update_features(workspace_slug=workspace_slug, data=data)
