"""Plane client initialization for MCP server."""

import os
from typing import NamedTuple

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

from plane_mcp.moneta.apitoken import build_plane_client
from plane_mcp.moneta.client import bearer_for
from plane_mcp.moneta.workspace import resolve_workspace

logger = get_logger(__name__)


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def get_plane_client_context() -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Authentication is handled by the PlaneOAuthProvider, which supports:
    1. Environment variables (PLANE_API_KEY + PLANE_WORKSPACE_SLUG)
    2. HTTP headers (x-api-key + x-workspace-slug)
    3. OAuth access token

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (preferred for server-to-server calls)
    - PLANE_BASE_URL: Base URL for Plane API (fallback, default: https://api.plane.so)

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        ConfigurationError: If access token is not available or workspace slug is missing
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    env_workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None
    auth_method = "env"  # overwritten below when a request-scoped token is present
    claim_workspace_slug: str | None = None

    # Get access token from the OAuth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        claim_workspace_slug = stored_access_token.claims.get("workspace_slug")

        # For API key auth methods, use api_key parameter; for OAuth, use access_token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            # Moneta fork: forward the upstream Cognito id_token (when present)
            # instead of the FastMCP reference JWT, so Plane's mPass chain resolves
            # the same user as the web login. No-op for non-Cognito paths.
            access_token = bearer_for(token, stored_access_token.claims)

    # Moneta fork: resolve the target workspace. Precedence: the per-call
    # workspace_slug tool arg (injected by plane_mcp.moneta.inject and stashed in a
    # ContextVar) > the token's workspace_slug claim (PAT X-Workspace-Slug /
    # Plane-OAuth installation) > PLANE_WORKSPACE_SLUG env. Per-call means
    # concurrent Claude conversations stay isolated. Raises a guiding error if none.
    workspace_slug = resolve_workspace(claim_workspace_slug, env_workspace_slug)

    # Moneta fork: build the client. On the Cognito path this sends BOTH the
    # id_token Bearer (satisfies mPass on /api/v1) AND a minted Plane X-Api-Key
    # (satisfies plane-api's DRF APIKeyAuthentication, which ignores the Bearer).
    # Bearer-only (Plane-OAuth) or X-Api-Key-only (PAT / stdio env) otherwise.
    # See plane_mcp.moneta.apitoken.
    client = build_plane_client(
        base_url,
        api_key=api_key,
        access_token=access_token,
        claims=stored_access_token.claims if stored_access_token else None,
    )

    # Moneta fork: trace the resolved Plane auth context (no token values).
    logger.debug(
        "get_plane_client_context: auth_method=%s constructor=%s workspace_slug=%s base_url=%s",
        auth_method,
        "access_token" if access_token else "api_key",
        workspace_slug or "<none>",
        base_url,
    )

    return PlaneClientContext(
        client=client,
        workspace_slug=workspace_slug,
    )
