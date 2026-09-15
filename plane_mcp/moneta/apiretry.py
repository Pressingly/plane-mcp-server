"""Re-mint and retry once when Plane rejects a minted ``X-Api-Key`` (Moneta fork).

Why this exists
---------------
``plane_mcp.moneta.apitoken`` mints a Plane API token per SSO user and caches it for
30 days. Nothing validated that cached key on the way out, and nothing evicted it on
a failure, so a key that stopped working stayed in the cache until the TTL expired.

That was survivable only because Plane's external API used to authenticate the
session **before** the API key: a dead key fell through to the oauth2-proxy-created
session and the call quietly succeeded. Plane PR #48 reorders those authenticators to
``[APIKeyAuthentication, BaseSessionAuthentication]`` so that token revocation
actually takes effect — correct, and it turns a silent fallback into a hard failure
for every tool call that user makes, for up to 30 days.

Keys go stale on their own, without anyone revoking anything: ``_revoke_existing()``
DELETEs every prior ``moneta-mcp`` token on each mint, so a second container minting
for the same user invalidates the key the first one still has cached.

Where the retry goes
--------------------
At the SDK's HTTP verbs, not at the tool. ``get_plane_client_context`` has ~130 call
sites, and retrying a whole tool would re-run everything it had already done. One
``BaseResource`` verb is exactly one HTTP request, so wrapping the five of them
retries the single rejected request and nothing else. ``BaseResource._headers()``
re-reads ``config.api_key`` on every call, so refreshing the shared ``Configuration``
is enough for the retry to carry the new key.

Why a 403 and not a 401
-----------------------
``APIKeyAuthentication`` defines no ``authenticate_header()``. DRF's
``APIView.handle_exception`` asks the **first** authenticator for that header to
decide the status, gets ``None``, and coerces ``AuthenticationFailed`` to **403**.
With the reorder that first authenticator is ``APIKeyAuthentication``, so a rejected
key arrives as 403.

Plane also returns 403 for a genuine permission denial, and the two are
indistinguishable by status alone — so the body decides, as a strict allow-list. The
request is re-issued only when the body positively says the token was rejected
(:func:`_is_token_rejection`); every other 403 propagates untouched with the cached
key left alone.

Classifying the other way round — retry unless the body looks like a denial — is the
trap here, for two reasons. Plane's ``/api/v1`` denials are hand-rolled
``Response({"error": ...}, status=403)``, not DRF's ``{"detail": ...}``, so a
deny-list misses them entirely; and it would also treat an empty body or Traefik HTML
as a rejected token. Getting that wrong is expensive rather than merely useless: a
re-mint calls ``_revoke_existing()``, which DELETEs every ``moneta-mcp`` token for
that user, so re-minting on an ordinary permission denial would revoke a live key and
leave every other replica holding a dead one.

The honest tradeoff: if Plane ever rewords that message, the retry stops firing and
the stale-key problem returns. That is the safe direction to fail, and the reason the
phrase is asserted in the tests.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from fastmcp.utilities.logging import get_logger
from plane.api.base_resource import BaseResource
from plane.errors.errors import HttpError

logger = get_logger(__name__)

_WRAPPED_MARKER = "_moneta_apiretry_wrapped"
_VERB_METHODS = ("_get", "_post", "_put", "_patch", "_delete")

# What DRF renders for AuthenticationFailed("Given API token is not valid"), which is
# the only 403 re-minting can fix. Matched positively: see _is_token_rejection.
_TOKEN_REJECTED_DETAIL = "given api token is not valid"


def _is_token_rejection(error: HttpError) -> bool:
    """True only when the body positively identifies a rejected API token.

    Deliberately an allow-list. Plane's ``/api/v1`` permission denials are hand-rolled
    ``Response({"error": ...}, status=403)`` rather than DRF's ``{"detail": ...}``, so
    anything that classified by *excluding* known denials would treat every
    unrecognised body — ``{"error": ...}``, empty, HTML from Traefik or oauth2-proxy —
    as a rejected token and re-mint on it.
    """
    payload = getattr(error, "response", None)
    if not isinstance(payload, dict):
        return False
    detail = payload.get("detail")
    return isinstance(detail, str) and _TOKEN_REJECTED_DETAIL in detail.lower()


def _minted_identity(config: Any) -> str | None:
    """Identity behind ``config.api_key``, or ``None`` if we did not mint it."""
    from plane_mcp.moneta.apitoken import MINTED_IDENTITY_ATTR

    identity = getattr(config, MINTED_IDENTITY_ATTR, None)
    return identity if isinstance(identity, str) and identity else None


def _retry_once(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: BaseResource, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except HttpError as error:
            if error.status_code != 403:
                raise
            identity = _minted_identity(self.config)
            if identity is None:
                raise
            stale = self.config.api_key
            if not stale:
                raise
            if not _is_token_rejection(error):
                logger.debug("api-token retry: 403 is not a token rejection, leaving the key alone")
                raise

            from plane_mcp.moneta.apitoken import refresh_api_key

            fresh = refresh_api_key(identity, stale)
            # `fresh == stale` is unreachable by construction (a successful mint always
            # returns a new token) and kept only as a backstop. The real classification
            # is _is_token_rejection above; do not mistake this for it.
            if not fresh or fresh == stale:
                logger.debug("api-token retry: no replacement key for identity=%s, surfacing the 403", identity)
                raise
            self.config.api_key = fresh
            logger.info("api-token retry: re-minted for identity=%s, retrying the request once", identity)
            return method(self, *args, **kwargs)

    setattr(wrapper, _WRAPPED_MARKER, True)
    return wrapper


def install() -> None:
    """Wrap ``BaseResource``'s HTTP verbs so a rejected minted key refreshes once.

    Idempotent, and safe to call on any request: re-running it leaves an already
    wrapped method alone. A failure here leaves the SDK unpatched and logs, rather
    than taking the server down over a retry path.
    """
    try:
        for name in _VERB_METHODS:
            method = getattr(BaseResource, name, None)
            if method is None or getattr(method, _WRAPPED_MARKER, False):
                continue
            setattr(BaseResource, name, _retry_once(method))
    except Exception as exc:  # noqa: BLE001 — never break a tool call over this
        logger.warning("apiretry.install: leaving plane-sdk unpatched (%s)", exc)
