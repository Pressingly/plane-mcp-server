"""Client refresh grant on the Cognito path: overlapping refreshes and a flaky upstream.

Prod evidence (2026-11-03, 2026-11-04): claude.ai sent three overlapping refreshes
with one FastMCP refresh token, each refreshed upstream, and the one that met a
502 came back ``invalid_grant``, so the client discarded its tokens and forced a
reconnect. Drives the real ``get_cognito_http_mcp()`` app over ASGI with a mocked
upstream token endpoint and a session seeded straight into the OAuth stores.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import shlex
import time
from typing import get_args

import httpx
import jwt
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.jwt_issuer import JWTIssuer
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, RefreshTokenMetadata, UpstreamTokenSet, _hash_token
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import RefreshToken, TokenError, TokenErrorCode
from mcp.shared.auth import OAuthToken

from plane_mcp.moneta import cognito, session_log
from plane_mcp.moneta import http as moneta_http
from plane_mcp.moneta.refresh import RecentRefreshes, is_transient_upstream_failure

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
UPSTREAM_TOKEN_ID = "upstream-session"
UPSTREAM_TOKEN_URL = "https://auth.example.test/oauth2/token"
_SIGNING_KEY = "k" * 32
SCENARIO_TIMEOUT_SECONDS = 10
BROWSER_ORIGIN = "https://inspector.example.test"

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
        "id_token": _jwt({"sub": "user-sub", "email": "user@example.test", "cognito:username": "mpass-1"}),
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

    async def refresh(self, refresh_token: str | None = None, headers: dict | None = None) -> httpx.Response:
        return await self.http.post(
            "/token",
            headers=headers,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token or self.refresh_token,
                "client_id": self.client_id,
            },
        )

    async def stored_upstream(self) -> UpstreamTokenSet:
        return await self.provider._upstream_token_store.get(key=UPSTREAM_TOKEN_ID)


class RejectingVerifier:
    """Stands in for Cognito JWKS validation, which would otherwise fetch keys over the network."""

    async def verify_token(self, token: str) -> None:
        return None


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
            mcp.auth._token_validator = RejectingVerifier()
            refresh_token = await _seed_session(mcp.auth, client_id)
            await asyncio.wait_for(
                scenario(Session(http, mcp.auth, client_id, refresh_token)), timeout=SCENARIO_TIMEOUT_SECONDS
            )

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

        failed = await session.refresh(headers={"Origin": BROWSER_ORIGIN})

        assert failed.status_code == 503
        assert failed.json()["error"] == "temporarily_unavailable"
        assert failed.headers["retry-after"]
        assert failed.headers["cache-control"] == "no-store"
        assert failed.headers["access-control-allow-origin"] == "*"
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


def _tokens(refresh_token: str = "new-rt", expires_in: int = 3600) -> OAuthToken:
    return OAuthToken(access_token="at", token_type="Bearer", expires_in=expires_in, refresh_token=refresh_token)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_recent_refreshes_forgets_a_replay_after_the_window():
    clock = FakeClock()
    recent = RecentRefreshes(window_seconds=10, clock=clock)
    recent.record(_refresh_token(), _tokens())

    clock.now = 9
    assert recent.tokens("rt", "client") is not None
    clock.now = 11
    assert recent.tokens("rt", "client") is None


def test_recent_refreshes_purges_expired_entries_on_the_next_record():
    clock = FakeClock()
    recent = RecentRefreshes(window_seconds=10, clock=clock)
    recent.record(_refresh_token(), _tokens())

    clock.now = 11
    recent.record(RefreshToken(token="rt-2", client_id="client", scopes=[], expires_at=None), _tokens("newer"))

    assert len(recent) == 1


def test_recent_refreshes_keeps_only_a_hash_of_the_presented_token():
    recent = RecentRefreshes()
    presented = "presented-refresh-token-secret"
    recent.record(RefreshToken(token=presented, client_id="client", scopes=[], expires_at=None), _tokens())

    assert presented not in repr(recent._replays)
    assert recent.refresh_token(presented, "client").token == presented


def test_recent_refreshes_never_replays_to_another_client():
    recent = RecentRefreshes()
    recent.record(_refresh_token("client"), _tokens())

    assert recent.tokens("rt", "other-client") is None
    assert recent.refresh_token("rt", "other-client") is None


def test_recent_refreshes_rebuilds_the_presented_refresh_token():
    recent = RecentRefreshes()
    recent.record(RefreshToken(token="rt", client_id="client", scopes=["openid"], expires_at=99), _tokens())

    assert recent.refresh_token("rt", "client") == RefreshToken(
        token="rt", client_id="client", scopes=["openid"], expires_at=99
    )


def test_recent_refreshes_ages_the_replayed_lifetime():
    clock = FakeClock()
    recent = RecentRefreshes(window_seconds=10, clock=clock)
    recent.record(_refresh_token(), _tokens(expires_in=5))

    clock.now = 3
    assert recent.tokens("rt", "client").expires_in == 2
    clock.now = 9
    assert recent.tokens("rt", "client").expires_in == 1


def test_recent_refreshes_follows_the_rotation_chain_to_the_newest_tokens():
    recent = RecentRefreshes()
    recent.record(_refresh_token(), _tokens("rt-2"))
    recent.record(RefreshToken(token="rt-2", client_id="client", scopes=[], expires_at=None), _tokens("rt-3"))

    assert recent.tokens("rt", "client").refresh_token == "rt-3"


def test_recent_refreshes_stays_within_capacity():
    recent = RecentRefreshes(capacity=1)
    recent.record(_refresh_token(), _tokens())
    recent.record(RefreshToken(token="rt-2", client_id="client", scopes=[], expires_at=None), _tokens("other"))

    assert recent.tokens("rt", "client") is None
    assert recent.tokens("rt-2", "client") is not None


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
    assert list(inspect.signature(OAuthProxy._try_transparent_refresh).parameters) == ["self", "upstream_token_set"]
    assert list(inspect.signature(OAuthProxy._get_refresh_lock).parameters) == ["self", "token_id"]
    assert "expected_token_use" in inspect.signature(JWTIssuer.verify_token).parameters
    assert "_get_refresh_lock" not in inspect.getsource(OAuthProxy.exchange_refresh_token), (
        "fastmcp now locks inside exchange_refresh_token; the override's anyio.Lock is not reentrant and would deadlock"
    )
    assert "temporarily_unavailable" not in get_args(TokenErrorCode)
    assert 'raise TokenError("invalid_grant", f"Upstream refresh failed: {e}") from e' in inspect.getsource(
        OAuthProxy.exchange_refresh_token
    )


@pytest.fixture
def warnings_log(caplog, monkeypatch):
    """Capture the provider's WARNING lines; the ``fastmcp`` logger does not propagate to root."""
    monkeypatch.setattr(logging.getLogger("fastmcp"), "propagate", True)
    caplog.set_level(logging.WARNING, logger=cognito.logger.name)
    return caplog


def _lines(caplog, message: str) -> list[str]:
    provider_records = (r for r in caplog.records if r.name == cognito.logger.name)
    return [r.getMessage() for r in provider_records if r.getMessage().startswith(message)]


def _fields(line: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in shlex.split(line.split(": ", 1)[1]))


def _fingerprint() -> str:
    return hashlib.sha256(UPSTREAM_TOKEN_ID.encode()).hexdigest()[:8]


async def _seed_access_token(session: Session, jti: str = "access-jti", mapped: bool = True) -> str:
    access_token = session.provider.jwt_issuer.issue_access_token(
        client_id=session.client_id,
        scopes=["openid"],
        jti=jti,
        expires_in=3600,
        upstream_claims={"id_token": _cognito_tokens()["id_token"]},
    )
    if mapped:
        await session.provider._jti_mapping_store.put(
            key=jti,
            value=JTIMapping(jti=jti, upstream_token_id=UPSTREAM_TOKEN_ID, created_at=time.time()),
            ttl=3600,
        )
    return access_token


def test_a_transient_refresh_failure_names_the_user_client_and_session_age(upstream, warnings_log):
    upstream.outcomes.append(httpx.Response(502, text="Bad Gateway"))

    async def scenario(session: Session):
        await session.refresh()
        [line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_CLIENT}: ")
        fields = _fields(line)

        assert fields["sub"] == "user-sub"
        assert fields["username"] == "mpass-1"
        assert "email" not in fields
        assert fields["client_id"] == session.client_id
        assert fields["session_age"].startswith("0d01h00m")
        assert fields["upstream_token"] == _fingerprint()
        assert fields["error"] == "HTTPStatusError"
        assert fields["status"] == "502"
        assert fields["oauth_error"] == "-"
        assert fields["retryable"] == "true"

    _run(scenario)


def test_an_upstream_rejection_is_logged_as_not_retryable_with_the_oauth_error(upstream, warnings_log):
    body = {"error": "invalid_grant", "error_description": "Refresh Token has expired"}
    upstream.outcomes.append(httpx.Response(400, json=body))

    async def scenario(session: Session):
        await session.refresh()
        [line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_CLIENT}: ")
        fields = _fields(line)

        assert fields["oauth_error"] == "invalid_grant"
        assert fields["detail"] == "Refresh Token has expired"
        assert fields["retryable"] == "false"
        assert fields["sub"] == "user-sub"

    _run(scenario)


def test_a_refresh_without_a_session_names_the_user_from_the_sealed_claims(upstream, warnings_log):
    async def scenario(session: Session):
        orphan = session.provider.jwt_issuer.issue_refresh_token(
            client_id=session.client_id,
            scopes=["openid"],
            jti="orphan-refresh-jti",
            expires_in=3600,
            upstream_claims={"id_token": _cognito_tokens()["id_token"]},
        )
        refresh_token = RefreshToken(token=orphan, client_id=session.client_id, scopes=["openid"], expires_at=None)
        client = await session.provider.get_client(session.client_id)

        with pytest.raises(TokenError):
            await session.provider.exchange_refresh_token(client, refresh_token, ["openid"])

        [line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_CLIENT}: ")
        fields = _fields(line)
        assert fields["username"] == "mpass-1"
        assert fields["client_id"] == session.client_id
        assert fields["upstream_token"] == "-"
        assert fields["retryable"] == "false"
        assert upstream.calls == 0

    _run(scenario)


def test_an_unknown_refresh_token_names_the_user(upstream, warnings_log):
    async def scenario(session: Session):
        unknown = session.provider.jwt_issuer.issue_refresh_token(
            client_id=session.client_id,
            scopes=["openid"],
            jti="unknown-refresh-jti",
            expires_in=3600,
            upstream_claims={"id_token": _cognito_tokens()["id_token"]},
        )

        rejected = await session.refresh(unknown)

        assert rejected.status_code == 401
        [line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_TOKEN_UNKNOWN}: ")
        assert _fields(line)["username"] == "mpass-1"
        assert _fields(line)["client_id"] == session.client_id

    _run(scenario)


def test_a_failed_transparent_refresh_and_its_401_are_warnings_with_identity(upstream, warnings_log):
    upstream.outcomes.append(httpx.ReadTimeout("upstream timed out"))

    async def scenario(session: Session):
        assert await session.provider.load_access_token(await _seed_access_token(session)) is None

        [refresh_line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_BACKGROUND}: ")
        [rejected_line] = _lines(warnings_log, f"{cognito.SESSION_REJECTED}: ")
        assert _fields(refresh_line)["error"] == "ReadTimeout"
        assert _fields(refresh_line)["upstream_token"] == _fingerprint()
        assert _fields(rejected_line)["client_id"] == session.client_id
        assert _fields(rejected_line)["username"] == "mpass-1"
        assert _fields(rejected_line)["refresh_token"] == "present"

    _run(scenario)


def test_a_valid_token_whose_session_is_gone_still_names_the_user(upstream, warnings_log):
    """A Valkey flush drops every mapping; each user's next request must say who died."""

    async def scenario(session: Session):
        access_token = await _seed_access_token(session, jti="orphaned-jti", mapped=False)

        assert await session.provider.load_access_token(access_token) is None

        [line] = _lines(warnings_log, f"{cognito.SESSION_REJECTED}: ")
        fields = _fields(line)
        assert fields["client_id"] == session.client_id
        assert fields["username"] == "mpass-1"
        assert fields["upstream_token"] == "-"
        assert fields["session_lookup"] == "missing"

    _run(scenario)


def test_a_slow_session_lookup_is_bounded(upstream, warnings_log, monkeypatch):
    monkeypatch.setattr(cognito, "SESSION_LOOKUP_TIMEOUT_SECONDS", 0.01)

    async def stalled(*_args):
        await asyncio.sleep(1)

    async def scenario(session: Session):
        access_token = await _seed_access_token(session)
        monkeypatch.setattr(session.provider, "_stored_session", stalled)
        upstream.outcomes.append(httpx.Response(400, json={"error": "invalid_grant"}))

        assert await session.provider.load_access_token(access_token) is None

        [line] = _lines(warnings_log, f"{cognito.SESSION_REJECTED}: ")
        assert _fields(line)["session_lookup"] == "timeout"
        assert _fields(line)["username"] == "mpass-1"

    _run(scenario)


def test_an_invalid_fastmcp_token_stays_out_of_the_warnings(upstream, warnings_log):
    async def scenario(session: Session):
        assert await session.provider.load_access_token("not-a-token") is None
        assert not _lines(warnings_log, cognito.SESSION_REJECTED)

    _run(scenario)


def _raise(*_args, **_kwargs):
    raise RuntimeError("formatter broke")


@pytest.mark.parametrize(
    ("failure", "status", "error"),
    [
        (httpx.Response(502, text="Bad Gateway"), 503, "temporarily_unavailable"),
        (httpx.Response(400, json={"error": "invalid_grant"}), 401, "invalid_grant"),
    ],
    ids=["transient", "rejected"],
)
def test_a_broken_formatter_never_changes_the_refresh_answer(
    upstream, warnings_log, monkeypatch, failure, status, error
):
    monkeypatch.setattr(session_log, "describe_failure", _raise)
    upstream.outcomes.append(failure)

    async def scenario(session: Session):
        answered = await session.refresh()

        assert answered.status_code == status
        assert answered.json()["error"] == error
        [line] = _lines(warnings_log, f"{cognito.SESSION_REFRESH_CLIENT}: ")
        assert _fields(line)["context_error"] == "RuntimeError"

    _run(scenario)


def test_a_broken_formatter_never_changes_a_rejected_request(upstream, warnings_log, monkeypatch):
    monkeypatch.setattr(session_log, "describe_failure", _raise)
    monkeypatch.setattr(session_log, "describe_session", _raise)
    upstream.outcomes.append(httpx.ReadTimeout("upstream timed out"))

    async def scenario(session: Session):
        assert await session.provider.load_access_token(await _seed_access_token(session)) is None
        assert len(_lines(warnings_log, "MCP session")) == 2

    _run(scenario)


def test_session_failure_lines_never_carry_a_token_or_an_email(upstream, warnings_log):
    upstream.outcomes.append(httpx.Response(502, text="Bad Gateway"))

    async def scenario(session: Session):
        stored = await session.stored_upstream()
        await session.refresh()
        await session.provider.load_access_token(await _seed_access_token(session))

        for secret in (session.refresh_token, stored.refresh_token, stored.access_token, UPSTREAM_TOKEN_ID):
            assert secret not in warnings_log.text
        assert warnings_log.records
        assert not [r for r in warnings_log.records if "@" in r.getMessage()]

    _run(scenario)


def test_format_age_spans_days():
    assert session_log.format_age(5 * 86400 + 2 * 3600 + 13 * 60 + 7) == "5d02h13m07s"
    assert session_log.format_age(-3) == "0d00h00m00s"


def test_render_quotes_spaced_values_and_marks_missing_ones():
    assert session_log.render({"a": None, "b": "x y", "c": 3}) == 'a=- b="x y" c=3'
