"""The PAT header route refuses proxied callers once it reaches plane-api directly (FOSS-642)."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from plane_mcp.moneta.header import HEADER_BASE_URL_ENV
from plane_mcp.moneta.http import build_app


async def _reached(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("reached")


def _served_app() -> Starlette:
    inner = Starlette(routes=[Route("/mcp", _reached, methods=["POST"])])
    return build_app(cognito_app=Starlette(), header_app=inner)


async def _post(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_served_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://plane-mcp:8211") as caller:
        return await caller.post("/http/api-key/mcp", headers=headers, json={})


@pytest.mark.anyio
@pytest.mark.parametrize("forwarded", ["X-Forwarded-For", "X-Forwarded-Host"])
async def test_proxied_caller_is_refused_when_the_route_is_internal(
    monkeypatch: pytest.MonkeyPatch, forwarded: str
) -> None:
    monkeypatch.setenv(HEADER_BASE_URL_ENV, "http://plane-api:8000")
    response = await _post({forwarded: "203.0.113.7"})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


@pytest.mark.anyio
async def test_direct_caller_reaches_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HEADER_BASE_URL_ENV, "http://plane-api:8000")
    response = await _post({})
    assert response.text == "reached"


@pytest.mark.anyio
async def test_proxied_caller_passes_while_the_route_uses_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HEADER_BASE_URL_ENV, raising=False)
    response = await _post({"X-Forwarded-For": "203.0.113.7"})
    assert response.text == "reached"
