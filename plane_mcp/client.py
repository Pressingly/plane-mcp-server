"""Plane client initialization for MCP server."""

import os
from typing import Any, NamedTuple

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

logger = get_logger(__name__)


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def _plane_bearer_for(token: str, claims: dict[str, Any] | None) -> str:
    """Pick the Cognito JWT to forward to Plane: ID token if available, else access token.

    oauth2-proxy in this stack uses ``--user-id-claim=cognito:username`` (set so the web
    cookie flow, which presents the **ID** token, resolves the right user). Cognito
    **access** tokens have only ``username`` (no ``cognito:`` prefix) and no ``email``
    claim, so forwarding the access token unchanged through Traefik+oauth2-proxy makes
    oauth2-proxy fall back to ``sub`` and Plane's ``ProxyAuthMiddleware`` resolves a
    different account than the web user.

    The matching ID token is attached to ``AccessToken.claims["id_token"]`` by
    :class:`plane_mcp.cognito_http._PlaneCognitoProvider.load_access_token`. It has the
    same ``aud`` / signature as the access token, so oauth2-proxy validates it cleanly
    and emits the right ``X-Auth-Request-User`` — same trust chain as the web flow,
    no header injection, no internal-port bypass.

    Falls back to ``token`` when no ID token is attached (e.g., header API-key auth,
    legacy stdio mode); local PAT setups still work in that case.
    """
    id_token = (claims or {}).get("id_token")
    if isinstance(id_token, str) and id_token:
        return id_token
    return token


def get_plane_client_context() -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Authentication is handled by the PlaneOAuthProvider, which supports:
    1. Environment variables (PLANE_API_KEY + PLANE_WORKSPACE_SLUG)
    2. HTTP headers (x-api-key + x-workspace-slug)
    3. OAuth access token (Cognito browser flow — swapped for the matching ID token
       so oauth2-proxy can resolve the user via ``cognito:username``)

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (skips Traefik+oauth2-proxy;
      only safe inside the same trust boundary, e.g. local dev with PAT auth).
    - PLANE_BASE_URL: Public Plane URL fronted by Traefik+oauth2-proxy (default for
      Cognito browser auth so all calls go through the same chain as the web UI).

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        ConfigurationError: If access token is not available or workspace slug is missing
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token: str | None = None

    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        claim_workspace = stored_access_token.claims.get("workspace_slug", "")
        if claim_workspace:
            workspace_slug = claim_workspace

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

    return PlaneClientContext(
        client=client,
        workspace_slug=workspace_slug,
    )
