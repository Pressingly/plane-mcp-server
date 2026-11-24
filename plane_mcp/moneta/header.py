"""Plane base URL for the per-request PAT route, ``/http/api-key`` (Moneta fork).

Callers inside the bundle (the access-agent) hold a user's own Plane API key
and reach plane-api without the mPass edge, so header mode can use its own
``PLANE_HEADER_INTERNAL_BASE_URL`` (e.g. ``http://plane-api:8000``). It is
opt-in, because a deployment must stop routing ``/http/api-key`` publicly
before a key alone is enough to reach Plane. Unset, header mode keeps the
shared base. ``PLANE_INTERNAL_BASE_URL`` stays the Cognito path's base, which
must keep going through the mPass edge.
"""

from __future__ import annotations

import os

HEADER_AUTH_METHOD = "api_key_header"
HEADER_BASE_URL_ENV = "PLANE_HEADER_INTERNAL_BASE_URL"
DEFAULT_PLANE_BASE_URL = "https://api.plane.so"


def shared_base_url() -> str:
    return os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", DEFAULT_PLANE_BASE_URL)


def header_base_url() -> str:
    return os.getenv(HEADER_BASE_URL_ENV, "").strip() or shared_base_url()


def base_url_for(auth_method: str) -> str:
    return header_base_url() if auth_method == HEADER_AUTH_METHOD else shared_base_url()
