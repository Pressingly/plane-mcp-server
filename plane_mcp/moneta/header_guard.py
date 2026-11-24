"""Keep public callers off the PAT header route (Moneta fork).

The ASGI side of :func:`plane_mcp.moneta.header.refuses_proxied_caller`: the
Cognito-mode ``/http/api-key`` mount answers 403 before the request reaches
FastMCP. ``PlaneHeaderAuthProvider`` applies the same check, so a mount
without this guard (upstream's http mode) refuses the caller too.
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from plane_mcp.moneta.header import refuses_proxied_caller

logger = logging.getLogger("fastmcp.plane_mcp.moneta.header_guard")


def _refusal() -> JSONResponse:
    return JSONResponse(
        {"error": "forbidden", "error_description": "The API-key route is internal to the bundle."},
        status_code=403,
    )


class InternalCallerGuard:
    """ASGI middleware refusing proxied requests while the header route is private."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and refuses_proxied_caller(Headers(scope=scope)):
            logger.warning("header route refused a proxied request: it is internal to the bundle")
            await _refusal()(scope, receive, send)
            return
        await self.app(scope, receive, send)
