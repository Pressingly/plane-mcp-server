"""Keep a client's refresh grant alive through concurrent and transient failures.

FastMCP's ``OAuthProxy.exchange_refresh_token`` takes no lock and turns *any*
upstream exception into ``invalid_grant``. claude.ai sends overlapping refreshes
with one FastMCP refresh token; each one refreshed upstream with the same Cognito
refresh token, and when one of them hit a 502 the client read ``invalid_grant``,
discarded its tokens and forced a manual reconnect although its siblings had
succeeded.

This module holds the pieces :class:`plane_mcp.moneta.cognito.PlaneCognitoProvider`
composes to fix that:

* :class:`RecentRefreshes` remembers, for :data:`REPLAY_WINDOW_SECONDS`, the tokens
  issued for a refresh token that was just rotated, so an overlapping duplicate
  receives the same answer instead of a second upstream call or a rejection.
  Duplicates already in flight are covered by the lock and the in-lock check; the
  window only has to outlast a duplicate whose ``load_refresh_token`` runs after
  the rotation. The trade-off: for that long, a rotated refresh token is not
  strictly one-time use. Presenting it again (same client only) returns the
  tokens already issued, never new ones, so it grants nothing the first caller
  did not already hold.
* :func:`is_transient_upstream_failure` separates a flaky upstream (transport
  errors, 5xx, 429) from a refresh token Cognito actually rejected.
* :func:`with_retryable_token_endpoint` turns :class:`UpstreamRefreshUnavailable`
  into ``503 temporarily_unavailable``. The MCP SDK's ``TokenErrorCode`` has no
  retryable code, so this cannot travel as a ``TokenError``.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

import httpx
from mcp.server.auth.provider import RefreshToken
from mcp.server.auth.routes import cors_middleware
from mcp.shared.auth import OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

REPLAY_WINDOW_SECONDS = 10
REPLAY_CAPACITY = 10_000
RETRY_AFTER_SECONDS = 5
TOKEN_PATH = "/token"
TOKEN_METHODS = ("POST", "OPTIONS")


class UpstreamRefreshUnavailable(Exception):
    """The upstream token endpoint failed transiently; the client should retry."""


def is_transient_upstream_failure(error: BaseException | None) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and _is_retryable_status(error.response.status_code)


def _is_retryable_status(status_code: int) -> bool:
    return status_code == HTTPStatus.TOO_MANY_REQUESTS or status_code >= HTTPStatus.INTERNAL_SERVER_ERROR


def raise_for_throttling(response: httpx.Response) -> httpx.Response:
    """authlib only raises for 5xx; a 429 would otherwise be parsed as an OAuth error."""
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        response.raise_for_status()
    return response


@dataclass(frozen=True)
class Replay:
    """What a rotated refresh token was exchanged for; the presented token itself is not kept."""

    client_id: str | None
    scopes: tuple[str, ...]
    expires_at: int | None
    tokens: OAuthToken
    recorded_at: float


class RecentRefreshes:
    """Tokens issued for recently rotated refresh tokens, keyed by a hash of the token.

    In-process, like FastMCP's own refresh locks: a second replica would not see it.
    """

    def __init__(
        self,
        window_seconds: float = REPLAY_WINDOW_SECONDS,
        capacity: int = REPLAY_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._capacity = capacity
        self._clock = clock
        self._replays: OrderedDict[str, Replay] = OrderedDict()

    def __len__(self) -> int:
        return len(self._replays)

    def record(self, refresh_token: RefreshToken, tokens: OAuthToken) -> None:
        self._purge_expired()
        key = _digest(refresh_token.token)
        self._replays.pop(key, None)
        self._replays[key] = Replay(
            client_id=refresh_token.client_id,
            scopes=tuple(refresh_token.scopes),
            expires_at=refresh_token.expires_at,
            tokens=tokens,
            recorded_at=self._clock(),
        )
        self._evict_overflow()

    def refresh_token(self, token: str, client_id: str | None) -> RefreshToken | None:
        """The presented refresh token as the SDK handler expects it, if it was just rotated."""
        replay = self._lookup(token, client_id)
        if replay is None:
            return None
        return RefreshToken(
            token=token, client_id=replay.client_id, scopes=list(replay.scopes), expires_at=replay.expires_at
        )

    def tokens(self, token: str, client_id: str | None) -> OAuthToken | None:
        """The newest tokens issued from ``token``'s rotation chain, aged to the time of asking."""
        newest = self._newest_in_chain(self._lookup(token, client_id), client_id)
        return self._aged(newest) if newest else None

    def _lookup(self, token: str, client_id: str | None) -> Replay | None:
        self._purge_expired()
        replay = self._replays.get(_digest(token))
        return replay if replay is not None and replay.client_id == client_id else None

    def _newest_in_chain(self, replay: Replay | None, client_id: str | None) -> Replay | None:
        for _ in range(len(self._replays)):
            successor = self._successor(replay, client_id) if replay else None
            if successor is None:
                break
            replay = successor
        return replay

    def _successor(self, replay: Replay, client_id: str | None) -> Replay | None:
        rotated = replay.tokens.refresh_token
        return self._lookup(rotated, client_id) if rotated else None

    def _aged(self, replay: Replay) -> OAuthToken:
        if replay.tokens.expires_in is None:
            return replay.tokens
        elapsed = int(self._clock() - replay.recorded_at)
        return replay.tokens.model_copy(update={"expires_in": max(replay.tokens.expires_in - elapsed, 1)})

    def _expired(self, replay: Replay) -> bool:
        return self._clock() - replay.recorded_at > self._window_seconds

    def _purge_expired(self) -> None:
        while self._replays and self._expired(next(iter(self._replays.values()))):
            self._replays.popitem(last=False)

    def _evict_overflow(self) -> None:
        while len(self._replays) > self._capacity:
            self._replays.popitem(last=False)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def temporarily_unavailable_response(description: str) -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": description},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Retry-After": str(RETRY_AFTER_SECONDS)},
    )


class RetryableTokenEndpoint:
    """ASGI wrapper answering :class:`UpstreamRefreshUnavailable` with a 503.

    A class rather than a function: Starlette's ``Route`` wraps plain functions as
    request handlers, while a callable instance is mounted as an ASGI app.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._app(scope, receive, send)
        except UpstreamRefreshUnavailable as unavailable:
            await _through_cors(temporarily_unavailable_response(str(unavailable)))(scope, receive, send)


def _through_cors(response: JSONResponse) -> ASGIApp:
    """The 503 carries the same CORS headers as every other ``/token`` answer.

    The wrapper sits outside FastMCP's ``cors_middleware``, so without this a
    browser client would see a CORS failure instead of a retryable error.
    """

    async def answer(_request: Request) -> JSONResponse:
        return response

    return cors_middleware(answer, list(TOKEN_METHODS))


def with_retryable_token_endpoint(routes: list[BaseRoute]) -> list[BaseRoute]:
    return [_retryable(route) if _is_token_route(route) else route for route in routes]


def _is_token_route(route: BaseRoute) -> bool:
    return isinstance(route, Route) and route.path == TOKEN_PATH


def _retryable(route: Route) -> Route:
    return Route(
        route.path,
        endpoint=RetryableTokenEndpoint(route.endpoint),
        methods=list(route.methods or []),
        name=route.name,
    )
