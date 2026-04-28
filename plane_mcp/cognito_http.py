"""AWS Cognito HTTP auth for streamable HTTP MCP.

When ``cognito_http_env_ready()`` is true, ``__main__`` serves MCP OAuth via
``AWSCognitoProvider`` at the **root** mount (``{MCP_BASE_URL}/mcp``).

Plane API calls use the validated session token as ``Authorization: Bearer`` on
``PlaneClient`` (see ``client.py``). If requests go through oauth2-proxy in front
of a Plane fork that trusts forwarded headers, the proxy can add
``X-Auth-Request-Access-Token`` upstream—no extra headers are set in this package.

When ``MCP_COGNITO_REDIRECT_URI`` is set, it is the **only** source for the full
Cognito allowlisted callback: authorize and token ``redirect_uri`` are exactly that
URL (see ``_idp_base_url_and_callback_path``). Otherwise the IdP uses
``MCP_BASE_URL`` + ``/auth/callback``.

The Cognito provider subclass strips the MCP ``resource`` parameter on the upstream
authorize request so token exchange works without a Cognito **resource server**
(when IdP admins only allowlisted ``/auth/callback``).

"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.stores.redis import RedisStore
from mcp.types import Icon
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from plane_mcp.server import get_header_mcp
from plane_mcp.tools import register_tools

logger = logging.getLogger(__name__)

REQUIRED_HTTP_ENV_VARS = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
)

# Default redirect URI patterns for DCR (localhost dev). Tighten in production via
# MCP_ALLOWED_CLIENT_REDIRECT_URIS (comma-separated).
_DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS: tuple[str, ...] = (
    "http://localhost:*/*",
    "http://127.0.0.1:*/*",
)

_oauth_kv_singleton: MemoryStore | RedisStore | None = None


class _AWSCognitoProviderNoUpstreamResource(AWSCognitoProvider):
    """Strip MCP ``resource`` when building the Cognito authorize URL.

    FastMCP forwards the client's ``resource`` query param to the IdP. Cognito
    then requires a matching resource server or token exchange returns
    ``invalid_grant``. Teams without Cognito console access often cannot add that
    entry; omitting ``resource`` on the upstream request matches a traditional
    Cognito app that only allowlists ``/auth/callback``.
    """

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict[str, Any]) -> str:
        tx = dict(transaction)
        if tx.get("resource"):
            logger.debug(
                "Cognito HTTP: omitting resource=%s on upstream authorize (Cognito resource server not configured)",
                tx.get("resource"),
            )
            tx["resource"] = None
        return super()._build_upstream_authorize_url(txn_id, tx)


def _idp_base_url_and_callback_path() -> tuple[str, str]:
    """``base_url`` and ``redirect_path`` for Cognito: ``redirect_uri`` is ``{base}{path}``.

    If ``MCP_COGNITO_REDIRECT_URI`` is set, it must match the Cognito app allowlist
    (e.g. ``https://foss-pm-mcp.example.com/auth/callback``). No other value is
    sent to Cognito for authorize or token exchange.

    If unset, ``MCP_BASE_URL`` (rtrim ``/``) and ``/auth/callback`` are used.
    """
    raw = os.getenv("MCP_COGNITO_REDIRECT_URI", "").strip()
    if not raw:
        base = os.environ["MCP_BASE_URL"].strip().rstrip("/")
        return base, "/auth/callback"
    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise ValueError("MCP_COGNITO_REDIRECT_URI must be an http(s) URL")
    if not p.netloc:
        raise ValueError("MCP_COGNITO_REDIRECT_URI must include a host")
    if p.params or p.query or p.fragment:
        raise ValueError("MCP_COGNITO_REDIRECT_URI must not contain query or fragment")
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # Cognito: no double slash, no trailing slash on path
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/") or "/"
    if path == "/":
        raise ValueError("MCP_COGNITO_REDIRECT_URI must include a path, e.g. /auth/callback")
    base = f"{p.scheme}://{p.netloc}"
    mcp = os.environ.get("MCP_BASE_URL", "").strip().rstrip("/")
    if mcp and mcp != base:
        logger.warning(
            "MCP_COGNITO_REDIRECT_URI base %s differs from MCP_BASE_URL %s; Cognito uses the redirect URI only",
            base,
            mcp,
        )
    return base, path


def cognito_http_env_ready() -> bool:
    return all(os.getenv(name, "").strip() for name in REQUIRED_HTTP_ENV_VARS)


def validate_cognito_http_env() -> None:
    missing = [name for name in REQUIRED_HTTP_ENV_VARS if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("http mode (Cognito) is missing required env vars: " + ", ".join(missing))


def _allowed_client_redirect_uris() -> list[str]:
    raw = os.getenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "").strip()
    if not raw:
        return list(_DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)
    return [uri.strip() for uri in raw.split(",") if uri.strip()]


def _oauth_client_storage() -> MemoryStore | RedisStore:
    global _oauth_kv_singleton
    if _oauth_kv_singleton is not None:
        return _oauth_kv_singleton
    redis_host = os.getenv("REDIS_HOST", "").strip()
    redis_port = os.getenv("REDIS_PORT", "").strip()
    if redis_host and redis_port:
        logger.info("Cognito HTTP: using Redis for OAuth client/token storage")
        _oauth_kv_singleton = RedisStore(host=redis_host, port=int(redis_port))
    else:
        logger.warning(
            "Cognito HTTP: using in-memory OAuth storage (set REDIS_HOST/REDIS_PORT for production)"
        )
        _oauth_kv_singleton = MemoryStore()
    return _oauth_kv_singleton


def _build_cognito_provider() -> AWSCognitoProvider:
    base, redirect_path = _idp_base_url_and_callback_path()
    kwargs = dict(
        user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
        aws_region=os.environ["COGNITO_AWS_REGION"],
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=os.environ["OIDC_CLIENT_SECRET"],
        base_url=base,
        redirect_path=redirect_path,
        required_scopes=["openid"],
        allowed_client_redirect_uris=_allowed_client_redirect_uris(),
        client_storage=_oauth_client_storage(),
    )
    # Prefer FastMCP-native flag when available (newer versions).
    try:
        return _AWSCognitoProviderNoUpstreamResource(**kwargs, forward_resource=False)  # type: ignore[call-arg]
    except TypeError:
        return _AWSCognitoProviderNoUpstreamResource(**kwargs)


def get_cognito_http_mcp() -> FastMCP:
    """Single FastMCP app: Cognito OAuth + Plane tools."""
    if not os.getenv("PLANE_BASE_URL", "").strip() and not os.getenv("PLANE_INTERNAL_BASE_URL", "").strip():
        logger.warning(
            "Cognito HTTP: PLANE_BASE_URL / PLANE_INTERNAL_BASE_URL unset — "
            "Plane API calls may target the wrong host."
        )

    mcp = FastMCP(
        "Plane MCP Server (Cognito http)",
        icons=[Icon(src="https://plane.so/favicon.ico", alt="Plane MCP Server")],
        website_url="https://plane.so",
        auth=_build_cognito_provider(),
    )
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(mcp)
    return mcp


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_cognito_http_starlette_app() -> Starlette:
    """Cognito MCP at ``/`` (``/mcp``); optional PAT mount at ``/http/api-key``."""
    validate_cognito_http_env()
    cognito_mcp = get_cognito_http_mcp()
    header_mcp = get_header_mcp()
    cognito_app = cognito_mcp.http_app(stateless_http=True)
    header_app = header_mcp.http_app(stateless_http=True)

    routes: list = [
        Route("/healthz", healthz, methods=["GET"]),
        Mount("/http/api-key", app=header_app),
        Mount("/", app=cognito_app),
    ]

    @asynccontextmanager
    async def lifespan(app):
        async with cognito_app.lifespan(cognito_app):
            async with header_app.lifespan(header_app):
                yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    base, rpath = _idp_base_url_and_callback_path()
    cognito_redirect_uri = f"{base}{rpath}"
    logger.info(
        "Cognito HTTP: MCP at %s/mcp; PAT at %s/http/api-key/mcp; GET %s/healthz",
        base,
        base,
        base,
    )
    logger.info(
        "Cognito HTTP: IdP (Cognito) redirect_uri = %s — allowlist this exact URL in the Cognito app client",
        cognito_redirect_uri,
    )
    return app
