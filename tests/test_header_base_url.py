"""PLANE_HEADER_INTERNAL_BASE_URL reaches only the PAT header route (FOSS-642).

The Cognito path keeps PLANE_INTERNAL_BASE_URL, because its token mint has to
go through the mPass edge.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken

import plane_mcp.auth.plane_header_auth_provider as header_provider_module
import plane_mcp.client as client_module
import plane_mcp.moneta.client as moneta_client
from plane_mcp.auth import PlaneHeaderAuthProvider
from plane_mcp.moneta.cognito import ID_TOKEN_KEY, UPSTREAM_CLAIMS_KEY
from plane_mcp.moneta.header import HEADER_BASE_URL_ENV, base_url_for, header_base_url

HEADER_URL = "http://plane-api:8000"
EDGE_URL = "https://pm.example.test"


def _token(**claims) -> AccessToken:
    return AccessToken(
        token="tok", client_id="c", scopes=["read", "write"], expires_at=int(time.time()) + 3600, claims=claims
    )


COGNITO_TOKEN = _token(**{UPSTREAM_CLAIMS_KEY: {ID_TOKEN_KEY: "idtok", "email": "u@example.com"}})
HEADER_TOKEN = _token(auth_method="api_key_header", workspace_slug="acme")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANE_INTERNAL_BASE_URL", EDGE_URL)
    monkeypatch.setenv("PLANE_BASE_URL", "https://pm.public.test")
    monkeypatch.setenv(HEADER_BASE_URL_ENV, HEADER_URL)
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "acme")
    monkeypatch.delenv("PLANE_API_KEY", raising=False)


def test_header_base_url_prefers_the_header_setting() -> None:
    assert header_base_url() == HEADER_URL


def test_header_base_url_falls_back_to_the_shared_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HEADER_BASE_URL_ENV, " ")
    assert header_base_url() == EDGE_URL


@pytest.mark.parametrize("auth_method", ["oauth", "api_key_env", "environment"])
def test_other_auth_methods_keep_the_shared_base(auth_method: str) -> None:
    assert base_url_for(auth_method) == EDGE_URL


def _capture_client(monkeypatch: pytest.MonkeyPatch, token: AccessToken | None) -> dict:
    captured: dict = {}

    def fake_build(base_url, **kwargs):
        captured["base_url"] = base_url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(client_module, "get_access_token", lambda: token)
    monkeypatch.setattr(client_module, "build_plane_client", fake_build)
    return captured


def test_header_connection_uses_the_header_base(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_client(monkeypatch, HEADER_TOKEN)
    client_module.get_plane_client_context()
    assert captured["base_url"] == HEADER_URL
    assert captured["api_key"] == "tok"


def test_cognito_connection_keeps_the_edge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_client(monkeypatch, COGNITO_TOKEN)
    client_module.get_plane_client_context()
    assert captured["base_url"] == EDGE_URL
    assert captured["access_token"] == "idtok"


def test_stdio_keeps_the_shared_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    captured = _capture_client(monkeypatch, None)
    client_module.get_plane_client_context()
    assert captured["base_url"] == EDGE_URL


def test_cognito_app_api_requests_keep_the_edge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(moneta_client, "get_access_token", lambda: COGNITO_TOKEN)
    base_url, headers = moneta_client.plane_request_auth()
    assert base_url == EDGE_URL
    assert headers["Authorization"] == "Bearer idtok"


def _plane_answering(monkeypatch: pytest.MonkeyPatch, status: int) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        header_provider_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


@pytest.mark.anyio
async def test_validator_checks_the_key_on_the_header_base(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _plane_answering(monkeypatch, 200)
    assert await PlaneHeaderAuthProvider()._validate_api_key("pat-key") is True
    (request,) = seen
    assert str(request.url) == f"{HEADER_URL}/api/v1/users/me/"
    assert request.headers["x-api-key"] == "pat-key"


@pytest.mark.anyio
async def test_validator_rejects_a_deleted_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _plane_answering(monkeypatch, 401)
    assert await PlaneHeaderAuthProvider()._validate_api_key("pat-key") is False
