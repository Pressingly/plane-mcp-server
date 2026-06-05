"""Identity bridge: forward the Cognito id_token to the Plane API (Moneta fork).

Upstream ``plane_mcp.client.get_plane_client_context`` sends the validated token
straight to Plane. In the devstack's Cognito HTTP mode that token is a FastMCP
reference JWT — useless to Plane. :func:`bearer_for` swaps in the upstream Cognito
**id_token** (captured by :class:`plane_mcp.moneta.cognito.PlaneCognitoProvider`),
which oauth2-proxy/mPass in front of Plane validates and maps to the same user as
the web login. Upstream ``client.py`` calls :func:`bearer_for` from a single
hook line; for non-Cognito paths it returns ``token`` unchanged.

:func:`plane_request_auth` resolves the same credentials for raw **app-API**
requests (e.g. ``list_workspaces`` → ``/api/users/me/workspaces/``), which the
``/api/v1``-scoped plane-sdk can't reach.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger

from plane_mcp.moneta.cognito import EMAIL_CLAIM, ID_TOKEN_KEY, UPSTREAM_CLAIMS_KEY

logger = get_logger(__name__)


def bearer_for(token: str, claims: dict[str, Any] | None) -> str:
    """Forward the upstream Cognito id_token when present, else ``token``.

    The Cognito id_token carries ``email`` / ``cognito:username``, which Plane's
    oauth2-proxy (mPass) keys on to resolve the same user as the web/cookie login.
    Falls back to ``token`` for the Plane-OAuth path (where ``token`` is the Plane
    access token the SDK should send as Bearer).
    """
    upstream = (claims or {}).get(UPSTREAM_CLAIMS_KEY)
    if isinstance(upstream, dict):
        id_token = upstream.get(ID_TOKEN_KEY)
        if isinstance(id_token, str) and id_token:
            logger.debug(
                "bearer_for: forwarding upstream Cognito id_token (len=%d email=%s)",
                len(id_token),
                upstream.get(EMAIL_CLAIM),
            )
            return id_token
    logger.debug("bearer_for: no upstream id_token in claims — forwarding the validated token (Plane-OAuth/PAT path)")
    return token


def _stored_access_token() -> AccessToken | None:
    """Validated token for the in-flight HTTP request, or ``None`` in stdio mode.

    ``get_access_token()`` raises ``RuntimeError`` outside an HTTP request scope.
    """
    try:
        return get_access_token()
    except RuntimeError:
        return None


def plane_request_auth() -> tuple[str, dict[str, str]]:
    """Return ``(plane_base_url, headers)`` for a raw Plane **app-API** request.

    Mirrors plane-sdk's ``BaseResource._headers`` (``X-Api-Key`` for a PAT,
    ``Authorization: Bearer`` for a token — the Cognito id_token when available).
    ``requests`` honors ``REQUESTS_CA_BUNDLE`` for the devstack's self-signed cert
    automatically.
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    api_key = os.getenv("PLANE_API_KEY", "")
    access_token: str | None = None

    stored = _stored_access_token()
    if stored:
        auth_method = stored.claims.get("auth_method", "oauth")
        token = stored.token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = bearer_for(token, stored.claims)
            api_key = ""  # never send both; Bearer wins on the token path

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    logger.debug(
        "plane_request_auth: base_url=%s auth=%s",
        base_url,
        "x-api-key" if api_key else ("bearer" if access_token else "none"),
    )
    return base_url, headers
