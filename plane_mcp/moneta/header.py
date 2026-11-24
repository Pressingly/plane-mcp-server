"""Plane base URL and caller checks for the per-request PAT route, ``/http/api-key`` (Moneta fork).

Callers inside the bundle (the access-agent) hold a user's own Plane API key
and reach plane-api without the mPass edge, so the Cognito-mode header route
can use its own ``PLANE_HEADER_INTERNAL_BASE_URL`` (e.g. ``http://plane-api:8000``).
It is opt-in, because a deployment must stop routing ``/http/api-key`` publicly
before a key alone is enough to reach Plane. It is ignored outside Cognito
mode (``COGNITO_USER_POOL_ID`` unset), where upstream mounts the header route
without the fork's guard. ``PLANE_INTERNAL_BASE_URL`` stays the Cognito path's
base, which must keep going through the mPass edge.

While the setting is present, a request carrying any forwarding header is
refused wherever the header route runs: Traefik adds ``X-Forwarded-For`` and
``X-Forwarded-Host`` to everything it proxies, and the access-agent calls the
container directly without any of them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

HEADER_AUTH_METHOD = "api_key_header"
HEADER_BASE_URL_ENV = "PLANE_HEADER_INTERNAL_BASE_URL"
COGNITO_POOL_ENV = "COGNITO_USER_POOL_ID"
DEFAULT_PLANE_BASE_URL = "https://api.plane.so"
FORWARDED_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


def shared_base_url() -> str:
    return os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", DEFAULT_PLANE_BASE_URL)


def _configured_header_base_url() -> str:
    return os.getenv(HEADER_BASE_URL_ENV, "").strip()


def _cognito_mode() -> bool:
    return bool(os.getenv(COGNITO_POOL_ENV, "").strip())


def header_route_is_private() -> bool:
    """True while the internal URL is configured: the header route then refuses proxied callers."""
    return bool(_configured_header_base_url())


def header_base_url() -> str:
    configured = _configured_header_base_url()
    return configured if configured and _cognito_mode() else shared_base_url()


def base_url_for(auth_method: str) -> str:
    return header_base_url() if auth_method == HEADER_AUTH_METHOD else shared_base_url()


def came_through_a_proxy(headers: Mapping[str, str]) -> bool:
    lowered = {name.lower() for name in headers}
    return any(name in lowered for name in FORWARDED_HEADERS)


def refuses_proxied_caller(headers: Mapping[str, str]) -> bool:
    return header_route_is_private() and came_through_a_proxy(headers)
