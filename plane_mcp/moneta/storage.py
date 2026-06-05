"""OAuth-state storage for the Cognito HTTP provider (Moneta fork).

FastMCP's ``AWSCognitoProvider`` (via ``OAuthProxy``) keeps six collections of
OAuth state — DCR client registrations, in-flight authorize transactions,
authorization codes, upstream Cognito access/refresh tokens, JTI mappings, and
refresh-token metadata. By default these live in an encrypted file tree inside
the container, which is wiped on ``docker compose down && up`` (every MCP client
re-OAuths) and can't be shared across replicas.

Setting ``MCP_OAUTH_STORAGE_URL`` swaps the file store for a Redis/Valkey backend
(devstack: ``redis://valkey:6379/12``), Fernet-wrapped with a key derived from
``OIDC_CLIENT_SECRET`` (confidential clients) or ``MCP_JWT_SIGNING_KEY``
(public/PKCE clients) so the on-disk RDB never holds plaintext tokens.

This is intentionally separate from upstream ``plane_mcp.storage`` (whose
``build_token_store`` backs the Plane-OAuth provider) to keep the fork's storage
concern out of the upstream module.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.utilities.logging import get_logger
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

logger = get_logger(__name__)

# Same salt FastMCP uses for the file-store encryption key
# (fastmcp/server/auth/oauth_proxy/proxy.py). Sharing it means rotating the
# chosen entropy source invalidates state in either backend consistently.
_STORAGE_KEY_SALT = "fastmcp-storage-encryption-key"


def build_oauth_storage() -> AsyncKeyValue | None:
    """Return the OAuth-state store for the Cognito HTTP provider.

    Returns ``None`` when ``MCP_OAUTH_STORAGE_URL`` is unset, signalling FastMCP
    to keep its default encrypted file store (back-compat for the image outside
    the devstack). When set to a ``redis://`` / ``rediss://`` URL, returns a
    :class:`RedisStore` wrapped in :class:`FernetEncryptionWrapper`.

    Raises ``ValueError`` when the URL scheme is unsupported, or when neither
    ``OIDC_CLIENT_SECRET`` nor ``MCP_JWT_SIGNING_KEY`` is set (without one of them
    the Fernet key cannot be derived deterministically).
    """
    raw = os.getenv("MCP_OAUTH_STORAGE_URL", "").strip()
    if not raw:
        logger.debug("MCP_OAUTH_STORAGE_URL unset — using FastMCP's default encrypted file store for OAuth state")
        return None

    parsed = urlparse(raw)
    # RedisStore wraps redis-py's from_url, which only speaks redis(s)://. The
    # valkey:// scheme would need ValkeyStore; the devstack points this at
    # redis://valkey:6379/12 (Valkey speaks the Redis protocol).
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError(f"MCP_OAUTH_STORAGE_URL scheme must be redis:// or rediss://, got {parsed.scheme!r}")

    # Prefer the upstream client secret when present (confidential Cognito
    # client). Fall back to MCP_JWT_SIGNING_KEY for public/PKCE clients.
    key_material = os.getenv("OIDC_CLIENT_SECRET", "").strip() or os.getenv("MCP_JWT_SIGNING_KEY", "").strip()
    if not key_material:
        raise ValueError(
            "MCP_OAUTH_STORAGE_URL is set but neither OIDC_CLIENT_SECRET nor "
            "MCP_JWT_SIGNING_KEY is set; the Fernet encryption key cannot be "
            "derived without one of them."
        )

    store = RedisStore(url=raw)
    encryption_key = derive_jwt_key(high_entropy_material=key_material, salt=_STORAGE_KEY_SALT)
    # Log host/db only — the URL may carry a password.
    logger.info(
        "OAuth state stored in redis://%s%s (Fernet-encrypted at rest)",
        parsed.hostname or "?",
        parsed.path or "/0",
    )
    return FernetEncryptionWrapper(
        key_value=store,
        fernet=Fernet(key=encryption_key),
        raise_on_decryption_error=False,
    )
