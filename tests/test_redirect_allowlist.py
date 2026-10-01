"""MCP_ALLOWED_CLIENT_REDIRECT_URIS enforcement on the Cognito HTTP path.

Drives the real ``get_cognito_http_mcp()`` app over HTTP (OIDC discovery stubbed,
OAuth state in a shared in-memory store) so each case exercises the provider
exactly as the server wires it.

Most clients in production were registered while the allowlist was empty, so the
cases that matter are records already in storage meeting an allowlist turned on
later. fastmcp 3.2.0 let ``/authorize`` without ``redirect_uri`` fall back to the
registered URI unchecked, which bypassed the allowlist for those records.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from key_value.aio.stores.memory import MemoryStore
from pydantic import AnyUrl
from starlette.testclient import TestClient

from plane_mcp.moneta import http as moneta_http

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
EVIL_CALLBACK = "https://evil.example/cb"
PROD_ALLOWLIST = f"{CLAUDE_CALLBACK},http://localhost:*/*,http://127.0.0.1:*/*"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

_COGNITO_ENV = {
    "MCP_BASE_URL": "https://pm-mcp.example.test",
    "COGNITO_USER_POOL_ID": "ap-southeast-1_test",
    "COGNITO_AWS_REGION": "ap-southeast-1",
    "OIDC_CLIENT_ID": "cognito-app-client",
    "OIDC_CLIENT_SECRET": "cognito-app-secret",
}

_ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
_DISCOVERY = OIDCConfiguration(
    strict=False,
    issuer=_ISSUER,
    authorization_endpoint="https://auth.example.test/oauth2/authorize",
    token_endpoint="https://auth.example.test/oauth2/token",
    jwks_uri=f"{_ISSUER}/.well-known/jwks.json",
)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def server(monkeypatch, store):
    """Return a factory building the Cognito app with a given allowlist over ``store``."""
    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", lambda *_args, **_kwargs: _DISCOVERY)
    monkeypatch.setattr(moneta_http, "build_oauth_storage", lambda: store)
    for key, value in _COGNITO_ENV.items():
        monkeypatch.setenv(key, value)

    def build(allowlist: str) -> tuple[TestClient, object]:
        monkeypatch.setenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", allowlist)
        mcp = moneta_http.get_cognito_http_mcp()
        return TestClient(mcp.http_app(stateless_http=True), follow_redirects=False), mcp.auth

    return build


def _register(client: TestClient, redirect_uri: str):
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def _registered_client_id(client: TestClient, redirect_uri: str) -> str:
    response = _register(client, redirect_uri)
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize(client: TestClient, client_id: str, redirect_uri: str | None = None):
    params = {
        "client_id": client_id,
        "response_type": "code",
        "state": "s",
        "code_challenge": PKCE_CHALLENGE,
        "code_challenge_method": "S256",
    }
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    return client.get("/authorize", params=params)


def _store_client(provider, redirect_uri: str, stored_patterns: list[str] | None) -> str:
    client_id = "client-registered-under-an-older-allowlist"
    record = ProxyDCRClient(
        client_id=client_id,
        client_secret=None,
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="none",
        allowed_redirect_uri_patterns=stored_patterns,
    )
    asyncio.run(provider._client_store.put(key=client_id, value=record))
    return client_id


def _assert_rejected(response) -> None:
    assert response.status_code == 400, f"expected 400, got {response.status_code}: {response.text}"
    assert "evil.example" not in response.headers.get("location", "")


def _assert_proceeds(response) -> None:
    assert response.status_code == 302, f"expected 302, got {response.status_code}: {response.text}"


def test_authorize_without_redirect_uri_rejects_disallowed_registered_uri(server):
    """The FOSS-465 bypass: omitting redirect_uri must not skip the allowlist."""
    open_server, _ = server("")
    client_id = _registered_client_id(open_server, EVIL_CALLBACK)

    locked_server, _ = server(PROD_ALLOWLIST)

    _assert_rejected(_authorize(locked_server, client_id))


def test_authorize_with_disallowed_redirect_uri_is_rejected(server):
    open_server, _ = server("")
    client_id = _registered_client_id(open_server, EVIL_CALLBACK)

    locked_server, _ = server(PROD_ALLOWLIST)

    _assert_rejected(_authorize(locked_server, client_id, EVIL_CALLBACK))


def test_register_rejects_disallowed_redirect_uri(server):
    locked_server, _ = server(PROD_ALLOWLIST)

    response = _register(locked_server, EVIL_CALLBACK)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize("send_redirect_uri", [True, False], ids=["explicit", "omitted"])
def test_claude_callback_is_allowed(server, send_redirect_uri):
    locked_server, _ = server(PROD_ALLOWLIST)
    client_id = _registered_client_id(locked_server, CLAUDE_CALLBACK)

    response = _authorize(locked_server, client_id, CLAUDE_CALLBACK if send_redirect_uri else None)

    _assert_proceeds(response)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_callback_on_any_port_is_allowed(server, host):
    locked_server, _ = server(PROD_ALLOWLIST)
    client_id = _registered_client_id(locked_server, f"http://{host}:33418/callback")

    _assert_proceeds(_authorize(locked_server, client_id, f"http://{host}:51234/callback"))


def test_current_allowlist_overrides_a_stored_pattern_list(server):
    """A client's stored patterns are replaced by the server's current allowlist.

    fastmcp 3.2.0 only back-filled clients stored with no patterns, so a client
    registered under an older allowlist kept that list forever.
    """
    _, provider = server(PROD_ALLOWLIST)
    client_id = _store_client(provider, CLAUDE_CALLBACK, stored_patterns=PROD_ALLOWLIST.split(","))

    narrowed_server, _ = server("http://localhost:*/*")

    _assert_rejected(_authorize(narrowed_server, client_id, CLAUDE_CALLBACK))


def test_stored_pattern_list_still_applies_while_allowlist_is_unset(server):
    open_server, provider = server("")
    client_id = _store_client(provider, CLAUDE_CALLBACK, stored_patterns=PROD_ALLOWLIST.split(","))

    _assert_proceeds(_authorize(open_server, client_id, CLAUDE_CALLBACK))


def test_unset_allowlist_still_requires_a_registered_redirect_uri(server):
    open_server, _ = server("")
    client_id = _registered_client_id(open_server, CLAUDE_CALLBACK)

    _assert_rejected(_authorize(open_server, client_id, EVIL_CALLBACK))
    _assert_proceeds(_authorize(open_server, client_id, CLAUDE_CALLBACK))
