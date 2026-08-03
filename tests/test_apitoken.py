"""Moneta fork — Plane API-token mint/cache + dual-header client builder.

Covers the fix for the `/api/v1` 401: the Cognito id_token Bearer satisfies mPass
but plane-api's DRF needs an `X-Api-Key`, so we mint a Plane API token and send
both headers.
"""

import plane_mcp.moneta.apitoken as apt
from plane_mcp.moneta.apitoken import (
    _cognito_identity,
    build_plane_client,
    plane_api_key,
)
from plane_mcp.moneta.cognito import (
    COGNITO_USERNAME_CLAIM,
    EMAIL_CLAIM,
    ID_TOKEN_KEY,
    UPSTREAM_CLAIMS_KEY,
)


def _claims(*, id_token="idtok", email=None, username=None):
    upstream = {}
    if id_token is not None:
        upstream[ID_TOKEN_KEY] = id_token
    if email is not None:
        upstream[EMAIL_CLAIM] = email
    if username is not None:
        upstream[COGNITO_USERNAME_CLAIM] = username
    return {UPSTREAM_CLAIMS_KEY: upstream}


# --- _cognito_identity -------------------------------------------------------


def test_identity_prefers_real_email():
    assert _cognito_identity(_claims(email="user@acme.com", username="1020")) == "user@acme.com"


def test_identity_falls_back_to_username_for_placeholder_email():
    # Federated users often carry a placeholder email (e.g. cognito:default_val).
    assert _cognito_identity(_claims(email="cognito:default_val", username="1020")) == "1020"


def test_identity_none_without_id_token():
    assert _cognito_identity(_claims(id_token=None, username="1020")) is None


def test_identity_none_without_upstream():
    assert _cognito_identity({}) is None


# --- plane_api_key: mint + cache ---------------------------------------------


def test_plane_api_key_none_for_non_cognito_path():
    assert plane_api_key(None) is None
    assert plane_api_key({}) is None  # no upstream id_token


def test_plane_api_key_mints_once_then_caches(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_STORAGE_URL", raising=False)  # → in-process cache
    apt._memory_cache.clear()
    mints = {"n": 0}

    def fake_mint():
        mints["n"] += 1
        return f"minted-{mints['n']}"

    monkeypatch.setattr(apt, "_mint", fake_mint)
    claims = _claims(username="1020")

    first = plane_api_key(claims)
    second = plane_api_key(claims)

    assert first == "minted-1"
    assert second == "minted-1"  # cache hit, not re-minted
    assert mints["n"] == 1


# --- _mint: app-route POST + best-effort revoke ------------------------------


def test_mint_posts_and_returns_token_and_revokes_prior(monkeypatch):
    monkeypatch.setattr(apt, "plane_request_auth", lambda: ("https://pm.example", {"Authorization": "Bearer idtok"}))
    calls = {"get": 0, "post": 0, "deleted": []}

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        calls["get"] += 1
        assert url == "https://pm.example/api/users/api-tokens/"
        return _Resp(200, [{"id": "old1", "label": "moneta-mcp"}, {"id": "keep", "label": "other"}])

    def fake_delete(url, headers=None, timeout=None):
        calls["deleted"].append(url)
        return _Resp(204, None)

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["post"] += 1
        assert url == "https://pm.example/api/users/api-tokens/"
        assert json["label"] == "moneta-mcp"
        return _Resp(201, {"token": "plane_pat_abc", "id": "new"})

    monkeypatch.setattr(apt.requests, "get", fake_get)
    monkeypatch.setattr(apt.requests, "delete", fake_delete)
    monkeypatch.setattr(apt.requests, "post", fake_post)

    token = apt._mint()

    assert token == "plane_pat_abc"
    assert calls["post"] == 1
    # Only our own prior token revoked; the 'other'-labelled one is left alone.
    assert calls["deleted"] == ["https://pm.example/api/users/api-tokens/old1/"]


def test_mint_returns_none_on_error_status(monkeypatch):
    monkeypatch.setattr(apt, "plane_request_auth", lambda: ("https://pm.example", {}))
    monkeypatch.setattr(apt, "_revoke_existing", lambda *a, **k: None)

    class _Resp:
        status_code = 403
        text = "forbidden"

        def json(self):
            return {}

    monkeypatch.setattr(apt.requests, "post", lambda *a, **k: _Resp())
    assert apt._mint() is None


# --- build_plane_client: dual-header on the Cognito path ----------------------


def test_build_client_sends_both_headers_on_cognito_path(monkeypatch):
    monkeypatch.setattr(apt, "plane_api_key", lambda claims: "minted-key")

    client = build_plane_client(
        "https://pm.example",
        api_key="",
        access_token="idtok",  # the id_token from bearer_for
        claims=_claims(username="1020"),
    )

    # Every resource shares one Configuration → both headers on each call.
    headers = client.projects._headers()
    assert headers["Authorization"] == "Bearer idtok"  # mPass
    assert headers["X-Api-Key"] == "minted-key"  # plane-api DRF


def test_build_client_bearer_only_when_no_minted_key(monkeypatch):
    monkeypatch.setattr(apt, "plane_api_key", lambda claims: None)

    client = build_plane_client(
        "https://pm.example",
        api_key="",
        access_token="idtok",
        claims=_claims(username="1020"),
    )

    headers = client.projects._headers()
    assert headers["Authorization"] == "Bearer idtok"
    assert "X-Api-Key" not in headers


def test_build_client_api_key_only_for_stdio_env():
    client = build_plane_client(
        "https://pm.example",
        api_key="pat-xyz",
        access_token=None,
        claims=None,
    )

    headers = client.projects._headers()
    assert headers["X-Api-Key"] == "pat-xyz"
    assert "Authorization" not in headers
