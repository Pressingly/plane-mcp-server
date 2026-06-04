"""Plane client initialization for MCP server."""

import os
from typing import Any, NamedTuple

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient
from plane.errors import ConfigurationError

logger = get_logger(__name__)


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def _plane_bearer_for(token: str, claims: dict[str, Any] | None) -> str:
    """Return the Cognito ID token from claims when present, otherwise the access token.

    Cognito ID tokens carry ``cognito:username`` which oauth2-proxy needs to match the
    web cookie flow. Falls back to ``token`` for Plane OAuth / PAT / stdio paths.
    """
    id_token = (claims or {}).get("id_token")
    if isinstance(id_token, str) and id_token:
        logger.info("Plane bearer: forwarding upstream Cognito id_token (len=%d)", len(id_token))
        return id_token
    return token


def get_plane_client_context(workspace_slug_from_client: str | None = None) -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Workspace slug precedence: ``workspace_slug_from_client`` > token claim > ``PLANE_WORKSPACE_SLUG``.

    Raises:
        ConfigurationError: If no workspace slug can be resolved.
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    # Get access token from the OAuth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        claim_workspace = stored_access_token.claims.get("workspace_slug", "")
        if claim_workspace:
            workspace_slug = claim_workspace

        # For API key auth methods, use api_key parameter; for OAuth, use access_token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = _plane_bearer_for(token, stored_access_token.claims)

    if access_token:
        client = PlaneClient(
            base_url=base_url,
            access_token=access_token,
        )
    else:
        client = PlaneClient(
            base_url=base_url,
            api_key=api_key,
        )

    slug = (workspace_slug_from_client or workspace_slug or "").strip()

    if not slug:
        raise ConfigurationError(
            "Workspace slug is required for Plane API calls but none was resolved. "
            "Pass workspace_slug on the tool (use list_workspaces to get the slug, e.g. 'arbisofttt'), "
            "set PLANE_WORKSPACE_SLUG for stdio, or authenticate with PAT and header X-Workspace-Slug. "
            "Without a slug, URLs look like /api/v1/workspaces/work-items/... and Plane returns 404."
        )

    return PlaneClientContext(
        client=client,
        workspace_slug=slug,
    )
