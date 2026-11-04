"""Keep a client's refresh grant alive through concurrent and transient failures.

FastMCP's ``OAuthProxy.exchange_refresh_token`` takes no lock and turns *any*
upstream exception into ``invalid_grant``. claude.ai sends overlapping refreshes
with one FastMCP refresh token; each one refreshed upstream with the same Cognito
refresh token, and when one of them hit a 502 the client read ``invalid_grant``,
discarded its tokens and forced a manual reconnect although its siblings had
succeeded.

This module holds the pieces :class:`plane_mcp.moneta.cognito.PlaneCognitoProvider`
composes to fix that:

* :class:`RecentRefreshes` remembers, for a short window, the tokens issued for a
  refresh token that was just rotated, so an overlapping duplicate receives the
  same answer instead of a second upstream call or a rejection.
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
from mcp.shared.auth import OAuthToken
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

REPLAY_WINDOW_SECONDS = 60
REPLAY_CAPACITY = 10_000
RETRY_AFTER_SECONDS = 5
TOKEN_PATH = "/token"


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
    refresh_token: RefreshToken
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

    def record(self, refresh_token: RefreshToken, tokens: OAuthToken) -> None:
        self._replays[_digest(refresh_token.token)] = Replay(refresh_token, tokens, self._clock())
        self._evict_overflow()

    def lookup(self, token: str, client_id: str | None) -> Replay | None:
        replay = self._replays.get(_digest(token))
        if replay is None or replay.refresh_token.client_id != client_id or self._expired(replay):
            return None
        return replay

    def _expired(self, replay: Replay) -> bool:
        return self._clock() - replay.recorded_at > self._window_seconds

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
            await temporarily_unavailable_response(str(unavailable))(scope, receive, send)


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
