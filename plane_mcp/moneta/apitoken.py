"""Plane API-token bridge for the Cognito path (Moneta fork).

Why this exists
---------------
Plane's **external** API (``/api/v1/*`` — every ``plane-sdk`` call) authenticates
ONLY via the ``X-Api-Key`` header against a Plane ``APIToken`` row
(``plane/api/middleware/api_authentication.py``). It does **not** accept the
Cognito id_token Bearer that mPass validates for the app routes. So in Cognito
HTTP mode the plane-sdk tools 401 at the DRF layer even though ``list_workspaces``
(a raw **app-route** call, ``/api/users/...``) works.

Two auth layers actually stack on ``/api/v1`` in the devstack (Traefik router
``plane-secure``, priority 10):

1. **mPass** (oauth2-proxy ForwardAuth) — satisfied by the id_token Bearer
   (``OAUTH2_PROXY_SKIP_JWT_BEARER_TOKENS``); this is why the request reaches DRF.
2. **plane-api DRF** (``APIKeyAuthentication``) — needs ``X-Api-Key``.

Bridge: mint a personal Plane API token for the SSO-resolved user once (app route
``POST /api/users/api-tokens/``, id_token relayed through mPass — the same path
``list_workspaces`` uses), cache it, and have the SDK send **both** headers: the
id_token Bearer (for mPass) and the minted ``X-Api-Key`` (for DRF). plane-sdk
forbids passing both credentials at construction, but all its resources share one
mutable ``Configuration``, so we set ``config.api_key`` after building the client.

The raw token is returned by Plane only at create time
(``APITokenReadSerializer`` excludes it), so we cache it — Fernet-encrypted in
Valkey when ``MCP_OAUTH_STORAGE_URL`` is set, else in-process — to avoid minting
one per call.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

from plane_mcp.moneta.client import plane_request_auth
from plane_mcp.moneta.cognito import (
    COGNITO_USERNAME_CLAIM,
    EMAIL_CLAIM,
    ID_TOKEN_KEY,
    UPSTREAM_CLAIMS_KEY,
)

logger = get_logger(__name__)

# Label stamped on tokens we mint, so we can recognize/replace our own.
_TOKEN_LABEL = "moneta-mcp"
_TOKEN_DESCRIPTION = "Auto-minted by plane-mcp for SSO /api/v1 access"
_CACHE_PREFIX = "plane-mcp:api-token:"
# Plane tokens are non-expiring (expired_at=None); re-minted on cache eviction.
_CACHE_TTL_SECONDS = 30 * 24 * 3600
# Distinct from storage.py's OAuth-state salt so the two key spaces never collide.
_KEY_SALT = "plane-mcp-api-token-cache"
_REQUEST_TIMEOUT = 30.0

# Fallback when MCP_OAUTH_STORAGE_URL is unset (stdio / no Valkey). Process-local;
# lost on restart, which only forces a re-mint.
_memory_cache: dict[str, str] = {}


def _cognito_identity(claims: dict[str, Any]) -> str | None:
    """Stable per-user cache key for the Cognito path, or ``None`` if not it.

    Prefers a real email; federated users frequently carry a placeholder/absent
    email (e.g. ``cognito:default_val``), so falls back to ``cognito:username``.
    Returns ``None`` when there is no upstream id_token — i.e. the PAT / Plane-OAuth
    paths, which keep their own credential and never mint.
    """
    upstream = claims.get(UPSTREAM_CLAIMS_KEY)
    if not isinstance(upstream, dict):
        return None
    if not isinstance(upstream.get(ID_TOKEN_KEY), str) or not upstream.get(ID_TOKEN_KEY):
        return None
    email = upstream.get(EMAIL_CLAIM)
    if isinstance(email, str) and "@" in email:
        return email
    username = upstream.get(COGNITO_USERNAME_CLAIM)
    if isinstance(username, str) and username:
        return username
    return None


def _redis_client() -> Any | None:
    """Sync redis client for ``MCP_OAUTH_STORAGE_URL``, or ``None`` for memory cache."""
    url = os.getenv("MCP_OAUTH_STORAGE_URL", "").strip()
    if not url or urlparse(url).scheme not in {"redis", "rediss"}:
        return None
    import redis  # local: only when a redis URL is configured

    return redis.Redis.from_url(url)


def _fernet() -> Fernet | None:
    """Fernet built from the same key material as the OAuth-state store, or ``None``."""
    key_material = os.getenv("OIDC_CLIENT_SECRET", "").strip() or os.getenv("MCP_JWT_SIGNING_KEY", "").strip()
    if not key_material:
        return None
    return Fernet(key=derive_jwt_key(high_entropy_material=key_material, salt=_KEY_SALT))


def _cache_get(identity: str) -> str | None:
    client = _redis_client()
    if client is None:
        return _memory_cache.get(identity)
    try:
        raw = client.get(_CACHE_PREFIX + identity)
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.warning("api-token cache read failed (%s); will mint", exc)
        return None
    if not raw:
        return None
    fernet = _fernet()
    if fernet is None:
        return raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return fernet.decrypt(raw).decode()
    except Exception as exc:  # noqa: BLE001 — stale/rotated key → re-mint
        logger.warning("api-token cache decrypt failed (%s); will re-mint", exc)
        return None


def _cache_set(identity: str, token: str) -> None:
    client = _redis_client()
    if client is None:
        _memory_cache[identity] = token
        return
    fernet = _fernet()
    value: bytes = fernet.encrypt(token.encode()) if fernet else token.encode()
    try:
        client.set(_CACHE_PREFIX + identity, value, ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fall back to memory
        logger.warning("api-token cache write failed (%s); caching in-process", exc)
        _memory_cache[identity] = token


def _revoke_existing(base_url: str, headers: dict[str, str]) -> None:
    """Best-effort delete of prior ``moneta-mcp``-labelled tokens for this user.

    Keeps exactly one active minted token per user instead of leaking a new
    full-access token on every cache eviction / container restart. Failures are
    swallowed — minting proceeds regardless.
    """
    list_url = f"{base_url.rstrip('/')}/api/users/api-tokens/"
    try:
        resp = requests.get(list_url, headers=headers, timeout=_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return
        tokens = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("api-token revoke: list failed (%s); skipping cleanup", exc)
        return
    if not isinstance(tokens, list):
        return
    for entry in tokens:
        if not isinstance(entry, dict) or entry.get("label") != _TOKEN_LABEL:
            continue
        pk = entry.get("id")
        if not pk:
            continue
        try:
            requests.delete(f"{list_url}{pk}/", headers=headers, timeout=_REQUEST_TIMEOUT)
            logger.debug("api-token revoke: deleted prior token id=%s", pk)
        except Exception as exc:  # noqa: BLE001
            logger.debug("api-token revoke: delete id=%s failed (%s)", pk, exc)


def _mint() -> str | None:
    """Create a Plane API token for the in-flight SSO user; return the raw token.

    Uses :func:`plane_request_auth` (id_token Bearer through mPass) against the app
    route ``POST /api/users/api-tokens/``. The POST response
    (``APITokenSerializer``) is the only place Plane returns the raw token.
    """
    base_url, headers = plane_request_auth()
    _revoke_existing(base_url, headers)
    create_url = f"{base_url.rstrip('/')}/api/users/api-tokens/"
    body = {"label": _TOKEN_LABEL, "description": _TOKEN_DESCRIPTION}
    try:
        resp = requests.post(create_url, json=body, headers=headers, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("api-token mint request failed: %s", exc)
        return None
    if resp.status_code not in (200, 201):
        logger.warning("api-token mint returned HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        token = (resp.json() or {}).get("token")
    except ValueError:
        logger.warning("api-token mint response was not JSON")
        return None
    if not isinstance(token, str) or not token:
        logger.warning("api-token mint response had no usable 'token' field")
        return None
    logger.debug("api-token mint: created new Plane API token (len=%d)", len(token))
    return token


def plane_api_key(claims: dict[str, Any] | None) -> str | None:
    """Return a Plane ``X-Api-Key`` for the Cognito SSO user (mint + cache).

    Returns ``None`` for non-Cognito paths (PAT / Plane-OAuth keep their own
    credential) and when minting fails (caller then falls back to the Bearer).
    """
    if not claims:
        return None
    identity = _cognito_identity(claims)
    if identity is None:
        return None
    cached = _cache_get(identity)
    if cached:
        logger.debug("plane_api_key: cache hit for identity=%s", identity)
        return cached
    token = _mint()
    if token:
        _cache_set(identity, token)
        logger.debug("plane_api_key: minted + cached Plane API token for identity=%s", identity)
    return token


def build_plane_client(
    base_url: str,
    *,
    api_key: str,
    access_token: str | None,
    claims: dict[str, Any] | None,
) -> PlaneClient:
    """Build a ``PlaneClient`` for ``get_plane_client_context``.

    On the Cognito path (``access_token`` is the id_token from
    :func:`plane_mcp.moneta.client.bearer_for` and a token can be minted) the
    client sends **both** headers: the id_token Bearer (for mPass on ``/api/v1``)
    and the minted ``X-Api-Key`` (for plane-api's DRF). plane-sdk forbids both at
    construction, so the key is set on the shared ``Configuration`` afterwards —
    every resource reads that same object.

    Otherwise behaves like upstream: Bearer-only (Plane-OAuth) or ``X-Api-Key``-only
    (PAT / stdio env).
    """
    minted_key = plane_api_key(claims) if access_token else None
    if minted_key and access_token:
        client = PlaneClient(base_url=base_url, access_token=access_token)
        client.config.api_key = minted_key  # dual-header: Bearer (mPass) + X-Api-Key (DRF)
        logger.debug("build_plane_client: dual-header (id_token Bearer + minted X-Api-Key)")
        return client
    if access_token:
        logger.debug("build_plane_client: Bearer-only (no minted key — Plane-OAuth/PAT path or mint failed)")
        return PlaneClient(base_url=base_url, access_token=access_token)
    logger.debug("build_plane_client: api_key-only (PAT / stdio env)")
    return PlaneClient(base_url=base_url, api_key=api_key)
