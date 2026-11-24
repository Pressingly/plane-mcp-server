"""Keep public callers off the PAT header route (Moneta fork).

With ``PLANE_HEADER_INTERNAL_BASE_URL`` set, ``/http/api-key`` reaches plane-api
past mPass and the corporate-ID gate, so the edge router must not serve it.
This guard is the server's own check: Traefik adds ``X-Forwarded-For`` and
``X-Forwarded-Host`` to every request it proxies, while the access-agent calls
the container directly and sends neither, so a request carrying one came
through the edge and is refused.
"""

from __future__ import annotations

import logging
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from plane_mcp.moneta.header import HEADER_BASE_URL_ENV

logger = logging.getLogger("fastmcp.plane_mcp.moneta.header_guard")

FORWARDED_HEADERS = ("x-forwarded-for", "x-forwarded-host")


def internal_route_enabled() -> bool:
    return bool(os.getenv(HEADER_BASE_URL_ENV, "").strip())


def came_through_a_proxy(headers: Headers) -> bool:
    return any(name in headers for name in FORWARDED_HEADERS)


def _refusal() -> JSONResponse:
    return JSONResponse(
        {"error": "forbidden", "error_description": "The API-key route is internal to the bundle."},
        status_code=403,
    )


class InternalCallerGuard:
    """ASGI middleware refusing proxied requests while the header route uses the internal URL."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and internal_route_enabled() and came_through_a_proxy(Headers(scope=scope)):
            logger.warning("header route refused a proxied request: it is internal to the bundle")
            await _refusal()(scope, receive, send)
            return
        await self.app(scope, receive, send)
