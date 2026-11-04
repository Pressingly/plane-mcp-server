"""Client refresh grant on the Cognito path: overlapping refreshes and a flaky upstream.

Prod evidence (2026-11-03, 2026-11-04): claude.ai sent three overlapping refreshes
with one FastMCP refresh token, each refreshed upstream, and the one that met a
502 came back ``invalid_grant``, so the client discarded its tokens and forced a
reconnect. Drives the real ``get_cognito_http_mcp()`` app over ASGI with a mocked
upstream token endpoint and a session seeded straight into the OAuth stores.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import get_args

import httpx
import jwt
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, RefreshTokenMetadata, UpstreamTokenSet, _hash_token
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import RefreshToken, TokenErrorCode
from mcp.shared.auth import OAuthToken

from plane_mcp.moneta import http as moneta_http
from plane_mcp.moneta.refresh import RecentRefreshes, is_transient_upstream_failure

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
UPSTREAM_TOKEN_ID = "upstream-session"
UPSTREAM_TOKEN_URL = "https://auth.example.test/oauth2/token"
_SIGNING_KEY = "k" * 32

_COGNITO_ENV = {
    "MCP_BASE_URL": "https://pm-mcp.example.test",
    "COGNITO_USER_POOL_ID": "ap-southeast-1_test",
    "COGNITO_AWS_REGION": "ap-southeast-1",
    "OIDC_CLIENT_ID": "cognito-app-client",
    "OIDC_CLIENT_SECRET": "cognito-app-secret",
    "MCP_ALLOWED_CLIENT_REDIRECT_URIS": CLAUDE_CALLBACK,
}
_ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
_DISCOVERY = OIDCConfiguration(
    strict=False,
    issuer=_ISSUER,
    authorization_endpoint="https://auth.example.test/oauth2/authorize",
    token_endpoint=UPSTREAM_TOKEN_URL,
    jwks_uri=f"{_ISSUER}/.well-known/jwks.json",
)


def _jwt(claims: dict) -> str:
    return jwt.encode(claims, _SIGNING_KEY)


def _cognito_tokens(lifetime: int = 3600) -> dict:
    return {
        "access_token": _jwt({"exp": int(time.time()) + lifetime, "sub": "user-sub"}),
        "id_token": _jwt({"email": "user@example.test", "cognito:username": "mpass-1"}),
        "token_type": "Bearer",
        "expires_in": lifetime,
    }


class Upstream:
    """Mocked bridge/Cognito token endpoint answering from a queue of outcomes."""

    def __init__(self) -> None:
        self.calls = 0
        self.outcomes: list[httpx.Response | Exception] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(0.05)
        outcome = self.outcomes.pop(0) if self.outcomes else httpx.Response(200, json=_cognito_tokens())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def upstream(monkeypatch) -> Upstream:
    mock = Upstream()
    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", lambda *_args, **_kwargs: _DISCOVERY)
    monkeypatch.setattr(moneta_http, "build_oauth_storage", lambda: MemoryStore())
    monkeypatch.setattr(
        AWSCognitoProvider,
        "_create_upstream_oauth_client",
        lambda self: AsyncOAuth2Client(client_id="cognito-app-client", transport=httpx.MockTransport(mock.handle)),
    )
    for key, value in _COGNITO_ENV.items():
        monkeypatch.setenv(key, value)
    return mock


async def _seed_session(provider, client_id: str) -> str:
    """Store one signed-in session and return the client's FastMCP refresh token."""
    now = time.time()
    await provider._upstream_token_store.put(
        key=UPSTREAM_TOKEN_ID,
        value=UpstreamTokenSet(
            upstream_token_id=UPSTREAM_TOKEN_ID,
            access_token=_jwt({"exp": int(now) - 10}),
            refresh_token="cognito-refresh-token",
            refresh_token_expires_at=None,
            expires_at=now - 10,
            token_type="Bearer",
            scope="openid",
            client_id=client_id,
            created_at=now - 3600,
            raw_token_data=_cognito_tokens(),
        ),
        ttl=86400,
    )
    refresh_jti = "refresh-jti"
    refresh_token = provider.jwt_issuer.issue_refresh_token(
        client_id=client_id, scopes=["openid"], jti=refresh_jti, expires_in=86400
    )
    await provider._jti_mapping_store.put(
        key=refresh_jti,
        value=JTIMapping(jti=refresh_jti, upstream_token_id=UPSTREAM_TOKEN_ID, created_at=now),
        ttl=86400,
    )
    await provider._refresh_token_store.put(
        key=_hash_token(refresh_token),
        value=RefreshTokenMetadata(client_id=client_id, scopes=["openid"], expires_at=int(now) + 86400, created_at=now),
        ttl=86400,
    )
    return refresh_token


class Session:
    """An HTTP client against the real app, with one registered MCP client signed in."""

    def __init__(self, http: httpx.AsyncClient, provider, client_id: str, refresh_token: str) -> None:
        self.http = http
        self.provider = provider
        self.client_id = client_id
        self.refresh_token = refresh_token

    async def refresh(self, refresh_token: str | None = None) -> httpx.Response:
        return await self.http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token or self.refresh_token,
                "client_id": self.client_id,
            },
        )

    async def stored_upstream(self) -> UpstreamTokenSet:
        return await self.provider._upstream_token_store.get(key=UPSTREAM_TOKEN_ID)


def _run(scenario) -> None:
    async def _main():
        mcp = moneta_http.get_cognito_http_mcp()
        app = mcp.http_app(stateless_http=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_COGNITO_ENV["MCP_BASE_URL"]) as http:
            registered = await http.post(
                "/register",
                json={
                    "redirect_uris": [CLAUDE_CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            assert registered.status_code == 201, registered.text
            client_id = registered.json()["client_id"]
            refresh_token = await _seed_session(mcp.auth, client_id)
            await scenario(Session(http, mcp.auth, client_id, refresh_token))

    asyncio.run(_main())


def test_overlapping_refreshes_make_one_upstream_call_and_agree(upstream):
    async def scenario(session: Session):
        responses = await asyncio.gather(*(session.refresh() for _ in range(3)))

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert len({r.json()["refresh_token"] for r in responses}) == 1
        assert len({r.json()["access_token"] for r in responses}) == 1
        assert upstream.calls == 1

    _run(scenario)


def test_a_duplicate_arriving_after_rotation_gets_the_same_tokens(upstream):
    async def scenario(session: Session):
        first = await session.refresh()
        late = await session.refresh()

        assert late.status_code == 200, late.text
        assert late.json() == first.json()
        assert upstream.calls == 1

    _run(scenario)


def test_the_rotated_refresh_token_refreshes_upstream_again(upstream):
    async def scenario(session: Session):
        first = await session.refresh()
        second = await session.refresh(first.json()["refresh_token"])

        assert second.status_code == 200, second.text
        assert second.json()["refresh_token"] != first.json()["refresh_token"]
        assert upstream.calls == 2

    _run(scenario)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(504, text="Gateway Timeout"),
        httpx.Response(429, json={"error": "slow_down"}),
        httpx.ReadTimeout("upstream timed out"),
        httpx.ConnectError("connection refused"),
    ],
    ids=["502", "504", "429", "timeout", "connect-error"],
)
def test_a_transient_upstream_failure_is_retryable_and_leaves_the_session_intact(upstream, failure):
    upstream.outcomes.append(failure)

    async def scenario(session: Session):
        before = await session.stored_upstream()

        failed = await session.refresh()

        assert failed.status_code == 503
        assert failed.json()["error"] == "temporarily_unavailable"
        assert failed.headers["retry-after"]
        assert failed.headers["cache-control"] == "no-store"
        assert await session.stored_upstream() == before

        retried = await session.refresh()

        assert retried.status_code == 200, retried.text
        assert upstream.calls == 2

    _run(scenario)


def test_an_upstream_rejection_is_still_invalid_grant(upstream):
    upstream.outcomes.append(httpx.Response(400, json={"error": "invalid_grant"}))

    async def scenario(session: Session):
        rejected = await session.refresh()

        assert rejected.status_code == 401
        assert rejected.json()["error"] == "invalid_grant"

    _run(scenario)


def test_a_failed_attempt_lets_the_queued_sibling_try_upstream_itself(upstream):
    upstream.outcomes.append(httpx.Response(502, text="Bad Gateway"))

    async def scenario(session: Session):
        responses = await asyncio.gather(session.refresh(), session.refresh())

        assert sorted(r.status_code for r in responses) == [200, 503]
        assert upstream.calls == 2

    _run(scenario)


def _refresh_token(client_id: str = "client") -> RefreshToken:
    return RefreshToken(token="rt", client_id=client_id, scopes=["openid"], expires_at=None)


def _tokens() -> OAuthToken:
    return OAuthToken(access_token="at", token_type="Bearer", expires_in=3600, refresh_token="new-rt")


def test_recent_refreshes_forgets_a_replay_after_the_window():
    now = [0.0]
    recent = RecentRefreshes(window_seconds=60, clock=lambda: now[0])
    recent.record(_refresh_token(), _tokens())

    now[0] = 59
    assert recent.lookup("rt", "client") is not None
    now[0] = 61
    assert recent.lookup("rt", "client") is None


def test_recent_refreshes_never_replays_to_another_client():
    recent = RecentRefreshes()
    recent.record(_refresh_token("client"), _tokens())

    assert recent.lookup("rt", "other-client") is None


def test_recent_refreshes_stays_within_capacity():
    recent = RecentRefreshes(capacity=1)
    recent.record(_refresh_token(), _tokens())
    recent.record(RefreshToken(token="rt-2", client_id="client", scopes=[], expires_at=None), _tokens())

    assert recent.lookup("rt", "client") is None
    assert recent.lookup("rt-2", "client") is not None


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (httpx.ReadTimeout("t"), True),
        (httpx.HTTPStatusError("e", request=None, response=httpx.Response(500)), True),
        (httpx.HTTPStatusError("e", request=None, response=httpx.Response(429)), True),
        (httpx.HTTPStatusError("e", request=None, response=httpx.Response(400)), False),
        (ValueError("not json"), False),
        (None, False),
    ],
)
def test_transient_classification(error, transient):
    assert is_transient_upstream_failure(error) is transient


def test_fastmcp_refresh_contract_still_matches_the_override():
    """Fails when fastmcp or the MCP SDK moves the seams the override relies on.

    If ``TokenErrorCode`` ever gains ``temporarily_unavailable``, raise it as a
    ``TokenError`` and drop the ``/token`` route wrapper.
    """
    assert list(inspect.signature(OAuthProxy.exchange_refresh_token).parameters) == [
        "self",
        "client",
        "refresh_token",
        "scopes",
    ]
    assert list(inspect.signature(OAuthProxy.load_refresh_token).parameters) == ["self", "client", "refresh_token"]
    assert callable(OAuthProxy._get_refresh_lock)
    assert "temporarily_unavailable" not in get_args(TokenErrorCode)
    assert 'raise TokenError("invalid_grant", f"Upstream refresh failed: {e}") from e' in inspect.getsource(
        OAuthProxy.exchange_refresh_token
    )
