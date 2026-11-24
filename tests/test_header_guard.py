"""The PAT header route refuses proxied callers while it is private (FOSS-642).

Both the Cognito-mode mount's ASGI guard and ``PlaneHeaderAuthProvider`` apply
the check, so upstream's unguarded mount refuses the caller too.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from plane_mcp.auth import PlaneHeaderAuthProvider
from plane_mcp.moneta.header import FORWARDED_HEADERS, HEADER_BASE_URL_ENV
from plane_mcp.moneta.http import build_app
from plane_mcp.server import get_header_mcp

FORWARDED = [name.title() for name in FORWARDED_HEADERS]


@pytest.fixture(autouse=True)
def _private_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HEADER_BASE_URL_ENV, "http://plane-api:8000")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_pool")


async def _reached(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("reached")


def _served_app() -> Starlette:
    inner = Starlette(routes=[Route("/mcp", _reached, methods=["POST"])])
    return build_app(cognito_app=Starlette(), header_app=inner)


async def _post(app: Starlette, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://plane-mcp:8211") as caller:
        return await caller.post("/http/api-key/mcp", headers=headers, json={})


@pytest.mark.anyio
@pytest.mark.parametrize("forwarded", FORWARDED)
async def test_proxied_caller_is_refused_when_the_route_is_private(forwarded: str) -> None:
    response = await _post(_served_app(), {forwarded: "203.0.113.7"})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


@pytest.mark.anyio
async def test_direct_caller_reaches_the_route() -> None:
    response = await _post(_served_app(), {})
    assert response.text == "reached"


@pytest.mark.anyio
async def test_proxied_caller_passes_while_the_route_uses_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HEADER_BASE_URL_ENV)
    response = await _post(_served_app(), {"X-Forwarded-For": "203.0.113.7"})
    assert response.text == "reached"


@pytest.mark.anyio
@pytest.mark.parametrize("forwarded", FORWARDED)
async def test_verify_token_refuses_a_proxied_caller(monkeypatch: pytest.MonkeyPatch, forwarded: str) -> None:
    import fastmcp.server.dependencies as dependencies

    headers = {"x-workspace-slug": "acme", forwarded.lower(): "203.0.113.7"}
    monkeypatch.setattr(dependencies, "get_http_headers", lambda include_all=False: headers)

    async def never_called(self, token: str) -> bool:
        raise AssertionError("a proxied caller's key must not be checked against Plane")

    monkeypatch.setattr(PlaneHeaderAuthProvider, "_validate_api_key", never_called)
    assert await PlaneHeaderAuthProvider().verify_token("pat-key") is None


@pytest.mark.anyio
@pytest.mark.parametrize("cognito_pool", ["us-east-1_pool", ""], ids=["cognito-mode", "fallback-mode"])
async def test_upstream_unguarded_mount_refuses_a_proxied_caller(
    monkeypatch: pytest.MonkeyPatch, cognito_pool: str
) -> None:
    """Upstream's http mode mounts the header app at /http/api-key without InternalCallerGuard."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", cognito_pool)
    checked_keys: list[str] = []

    async def record_check(self, token: str) -> bool:
        checked_keys.append(token)
        return True

    monkeypatch.setattr(PlaneHeaderAuthProvider, "_validate_api_key", record_check)
    header_app = get_header_mcp().http_app(stateless_http=True)
    app = Starlette(routes=[Mount("/http/api-key", app=header_app)], lifespan=header_app.lifespan)
    headers = {
        "Authorization": "Bearer pat-key",
        "x-workspace-slug": "acme",
        "X-Forwarded-For": "203.0.113.7",
        "Accept": "application/json, text/event-stream",
    }
    async with app.router.lifespan_context(app):
        response = await _post(app, headers)
    assert response.status_code == 401
    assert checked_keys == []
