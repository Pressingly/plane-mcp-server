"""AWS Cognito browser OAuth for streamable HTTP MCP (see README, "HTTP with AWS Cognito").

When ``cognito_http_env_ready()`` is true, ``python -m plane_mcp http`` serves MCP at
``{MCP_BASE_URL}/mcp`` using a public Cognito app client and ``MCP_JWT_SIGNING_KEY``.

``get_plane_client_context`` forwards the Cognito **ID token** to Plane when it is attached
to the MCP session (see ``plane_mcp.client._plane_bearer_for``). Set ``PLANE_WORKSPACE_SLUG``
when tokens have no ``workspace_slug`` claim.

PAT mount: ``{MCP_BASE_URL}/http/api-key/mcp``.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.aws import AWSCognitoProvider, AWSCognitoTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.stores.redis import RedisStore
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from mcp.types import Icon
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from plane_mcp.server import get_header_mcp
from plane_mcp.tools import register_tools

logger = logging.getLogger(f"fastmcp.{__name__}")

REQUIRED_HTTP_ENV_VARS = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
    "MCP_JWT_SIGNING_KEY",
)

_oauth_kv_singleton: MemoryStore | RedisStore | None = None

_DEFAULT_CALLBACK_PATH = "/auth/callback"


def _cognito_callback_base_and_path() -> tuple[str, str]:
    """Return ``(base_url, redirect_path)`` for Cognito ``redirect_uri`` (authorize + token)."""
    raw = os.getenv("MCP_COGNITO_REDIRECT_URI", "").strip() or os.getenv("PLANE_MCP_COGNITO_REDIRECT_URI", "").strip()
    if raw:
        p = urlparse(raw)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("MCP_COGNITO_REDIRECT_URI must be an absolute http(s) URL without query or fragment")
        if p.query or p.fragment:
            raise ValueError("MCP_COGNITO_REDIRECT_URI must not include query or fragment")
        path = p.path or _DEFAULT_CALLBACK_PATH
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/") or "/"
        if path == "/":
            raise ValueError("MCP_COGNITO_REDIRECT_URI must include a path (e.g. /auth/callback)")
        base = f"{p.scheme}://{p.netloc}".rstrip("/")
        mcp = os.environ.get("MCP_BASE_URL", "").strip().rstrip("/")
        if mcp and mcp != base:
            logger.warning(
                "MCP_COGNITO_REDIRECT_URI host %s differs from MCP_BASE_URL %s — Cognito uses the redirect URI only",
                base,
                mcp,
            )
        return base, path
    base = os.environ["MCP_BASE_URL"].strip().rstrip("/")
    return base, _DEFAULT_CALLBACK_PATH


def cognito_idp_redirect_uri() -> str:
    """Exact IdP callback URL sent to Cognito (for logs, Cognito console, tests)."""
    b, path = _cognito_callback_base_and_path()
    return f"{b}{path}"


class _AWSCognitoTokenVerifierUsernameFallback(AWSCognitoTokenVerifier):
    """Cognito access tokens often use ``cognito:username`` instead of ``username``."""

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await JWTVerifier.verify_token(self, token)
        if not access_token:
            return None
        raw = access_token.claims
        username = raw.get("username") or raw.get("cognito:username")
        cognito_claims = {
            "sub": raw.get("sub"),
            "username": username,
            "cognito:groups": raw.get("cognito:groups", []),
        }
        return AccessToken(
            token=access_token.token,
            client_id=access_token.client_id,
            scopes=access_token.scopes,
            expires_at=access_token.expires_at,
            claims=cognito_claims,
        )


class _PlaneCognitoProvider(AWSCognitoProvider):
    """Cognito OAuth: drop ``resource`` on upstream authorize when pool has no resource server.

    Also attaches the upstream Cognito **ID token** to the validated MCP ``AccessToken.claims``
    so tools can forward it to Plane API instead of the access token. Plane's Traefik
    chain runs ForwardAuth via oauth2-proxy with ``--user-id-claim=cognito:username``;
    Cognito **access** tokens have only ``username`` (no prefix), so plane-mcp would
    otherwise authenticate as ``sub@<smb>.com``. Forwarding the ID token (which has
    ``cognito:username``) gives plane-mcp the same Plane user as the web cookie flow
    without any oauth2-proxy / Plane / AWS changes. See ``plane_mcp/client.py``.
    """

    async def load_access_token(self, token: str) -> AccessToken | None:
        validated = await super().load_access_token(token)
        if validated is None:
            return None
        try:
            payload = self.jwt_issuer.verify_token(token)
            jti = payload.get("jti")
            if not jti:
                return validated
            jti_mapping = await self._jti_mapping_store.get(key=jti)
            if not jti_mapping:
                return validated
            upstream_id = getattr(jti_mapping, "upstream_token_id", None) or (
                jti_mapping.get("upstream_token_id") if isinstance(jti_mapping, dict) else None
            )
            if not upstream_id:
                return validated
            ts = await self._upstream_token_store.get(key=upstream_id)
            raw = getattr(ts, "raw_token_data", None) if ts else None
            if raw is None and isinstance(ts, dict):
                raw = ts.get("raw_token_data")
            id_token = (raw or {}).get("id_token") if isinstance(raw, dict) else None
            if not isinstance(id_token, str) or not id_token:
                return validated
            new_claims = dict(validated.claims or {})
            new_claims["id_token"] = id_token
            return validated.model_copy(update={"claims": new_claims})
        except Exception as exc:
            logger.warning("Cognito HTTP: failed to attach upstream id_token to claims: %s", exc)
            return validated

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Allow MCP clients whose ``resource`` URL does not match ``MCP_BASE_URL`` (e.g. localhost vs Traefik).

        FastMCP rejects mismatched ``resource`` with ``invalid_target`` before any redirect — Cursor often
        sends ``http://127.0.0.1:<port>/mcp`` while ``MCP_BASE_URL`` is ``https://foss-pm-mcp...``.
        Clear ``resource`` so consent/upstream Cognito flow runs; upstream authorize drops ``resource`` anyway.
        Set ``COGNITO_RELAX_OAUTH_RESOURCE_MISMATCH=false`` to enforce strict binding.
        """
        relax = os.getenv("COGNITO_RELAX_OAUTH_RESOURCE_MISMATCH", "true").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        server_resource = getattr(self, "_resource_url", None)
        client_resource = getattr(params, "resource", None)
        if (
            relax
            and client_resource is not None
            and server_resource is not None
            and str(client_resource) != str(server_resource)
        ):
            logger.warning(
                "Cognito OAuth: client resource %s != server MCP resource %s; continuing without "
                "resource binding (COGNITO_RELAX_OAUTH_RESOURCE_MISMATCH). Prefer aligning MCP_BASE_URL "
                "with the MCP URL your client uses.",
                client_resource,
                server_resource,
            )
            params = params.model_copy(update={"resource": None})
        return await super().authorize(client, params)

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict[str, Any]) -> str:
        tx = dict(transaction)
        if tx.get("resource"):
            logger.debug(
                "Cognito HTTP: omitting resource=%s on upstream authorize (no Cognito resource server)",
                tx.get("resource"),
            )
            tx["resource"] = None
        return super()._build_upstream_authorize_url(txn_id, tx)

    def get_token_verifier(
        self,
        *,
        algorithm: str | None = None,
        audience: str | None = None,
        required_scopes: list[str] | None = None,
        timeout_seconds: int | None = None,
    ):
        return _AWSCognitoTokenVerifierUsernameFallback(
            issuer=str(self.oidc_config.issuer),
            audience=audience,
            algorithm=algorithm,
            jwks_uri=str(self.oidc_config.jwks_uri),
            required_scopes=required_scopes,
        )


def cognito_http_env_missing() -> list[str]:
    """Return required Cognito env var names that are missing or blank."""
    return [name for name in REQUIRED_HTTP_ENV_VARS if not os.getenv(name, "").strip()]


def cognito_http_configuration_intended() -> bool:
    """True when Cognito pool/client id hints are set but env may be incomplete."""
    return bool(os.getenv("COGNITO_USER_POOL_ID", "").strip() or os.getenv("OIDC_CLIENT_ID", "").strip())


def cognito_http_env_ready() -> bool:
    return not cognito_http_env_missing()


def validate_cognito_http_env() -> None:
    missing = cognito_http_env_missing()
    if missing:
        raise ValueError("http mode (Cognito) is missing required env vars: " + ", ".join(missing))


def _allowed_client_redirect_uris() -> list[str] | None:
    """Redirect URI patterns for MCP OAuth Dynamic Client Registration (fnmatch wildcards).

    ``None`` means allow **any** client redirect URI (FastMCP default; needed for clients that use
    custom schemes such as ``cursor://``). Set ``MCP_ALLOWED_CLIENT_REDIRECT_URIS`` to a
    comma-separated list to restrict patterns in production.
    """
    raw = os.getenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "").strip()
    if not raw:
        return None
    return [uri.strip() for uri in raw.split(",") if uri.strip()]


def _oauth_client_storage() -> MemoryStore | RedisStore:
    global _oauth_kv_singleton
    if _oauth_kv_singleton is not None:
        return _oauth_kv_singleton
    redis_host = os.getenv("REDIS_HOST", "").strip()
    redis_port = os.getenv("REDIS_PORT", "").strip()
    if redis_host and redis_port:
        logger.info("Cognito HTTP: using Redis for OAuth storage")
        _oauth_kv_singleton = RedisStore(host=redis_host, port=int(redis_port))
    else:
        logger.warning("Cognito HTTP: in-memory OAuth storage (set REDIS_HOST/REDIS_PORT for production)")
        _oauth_kv_singleton = MemoryStore()
    return _oauth_kv_singleton


def _consent_enabled() -> bool:
    v = os.getenv("COGNITO_REQUIRE_CONSENT", "true").strip().lower()
    return v not in ("0", "false", "no")


def _build_cognito_provider() -> AWSCognitoProvider:
    base_url, redirect_path = _cognito_callback_base_and_path()
    redirect_patterns = _allowed_client_redirect_uris()
    if redirect_patterns is None:
        logger.info(
            "Cognito HTTP: MCP_ALLOWED_CLIENT_REDIRECT_URIS unset — any MCP client redirect URI allowed "
            "(needed for Cursor/custom OAuth schemes). Set MCP_ALLOWED_CLIENT_REDIRECT_URIS to restrict."
        )
    else:
        logger.info("Cognito HTTP: MCP client redirect URI patterns: %s", redirect_patterns)

    jwt_signing_key = os.environ["MCP_JWT_SIGNING_KEY"].strip()
    # FastMCP's AWSCognitoProvider requires non-empty client_secret; public Cognito apps
    # use COGNITO_TOKEN_ENDPOINT_AUTH_METHOD=none so this value is not sent to Cognito.
    kwargs: dict[str, Any] = dict(
        user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
        aws_region=os.environ["COGNITO_AWS_REGION"],
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=jwt_signing_key,
        base_url=base_url,
        redirect_path=redirect_path,
        required_scopes=["openid"],
        allowed_client_redirect_uris=redirect_patterns,
        client_storage=_oauth_client_storage(),
        require_authorization_consent=_consent_enabled(),
        jwt_signing_key=jwt_signing_key,
    )
    provider = _PlaneCognitoProvider(**kwargs)

    forward_pkce = os.getenv("COGNITO_OIDC_FORWARD_PKCE", "false").strip().lower() in ("1", "true", "yes")
    provider._forward_pkce = forward_pkce  # type: ignore[attr-defined]
    if forward_pkce:
        logger.info("Cognito HTTP: COGNITO_OIDC_FORWARD_PKCE=true (PKCE sent to Cognito authorize/token)")
    else:
        logger.info("Cognito HTTP: upstream PKCE off (default). Tools use Cognito access token as Plane Bearer.")

    auth_meth = os.getenv("COGNITO_TOKEN_ENDPOINT_AUTH_METHOD", "none").strip() or "none"
    provider._token_endpoint_auth_method = auth_meth  # type: ignore[attr-defined]
    logger.info("Cognito HTTP: public Cognito app client; token endpoint client authentication: %s", auth_meth)

    return provider


def get_cognito_http_mcp() -> FastMCP:
    if not os.getenv("PLANE_BASE_URL", "").strip() and not os.getenv("PLANE_INTERNAL_BASE_URL", "").strip():
        logger.warning("Cognito HTTP: PLANE_BASE_URL / PLANE_INTERNAL_BASE_URL unset — Plane API host may be wrong.")

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
    cb = cognito_idp_redirect_uri()
    base = os.environ["MCP_BASE_URL"].strip().rstrip("/")
    logger.info(
        "Cognito HTTP: MCP %s/mcp; PAT %s/http/api-key/mcp; health %s/healthz",
        base,
        base,
        base,
    )
    logger.info("Cognito HTTP: register this exact Callback URL in Cognito: %s", cb)
    return app
