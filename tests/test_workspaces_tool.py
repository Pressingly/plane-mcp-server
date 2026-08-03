"""Moneta fork — _fetch_workspaces: auth headers + response normalization."""

import plane_mcp.moneta.workspace as ws_mod
from plane_mcp.moneta.workspace import _fetch_workspaces


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_workspaces_pat_headers_and_list(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "pat-xyz")
    monkeypatch.delenv("PLANE_INTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("PLANE_BASE_URL", "https://pm.example")
    monkeypatch.delenv("PLANE_WORKSPACE_SLUG", raising=False)

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(
            [
                {"id": "1", "name": "Acme", "slug": "acme", "extra": "ignored"},
                {"id": "2", "name": "Beta", "slug": "beta"},
                "not-a-dict",
            ]
        )

    monkeypatch.setattr(ws_mod.requests, "get", fake_get)

    result = _fetch_workspaces()

    assert result == [
        {"id": "1", "name": "Acme", "slug": "acme"},
        {"id": "2", "name": "Beta", "slug": "beta"},
    ]
    assert captured["url"] == "https://pm.example/api/users/me/workspaces/"
    assert captured["headers"]["X-Api-Key"] == "pat-xyz"
    assert "Authorization" not in captured["headers"]


def test_fetch_workspaces_unwraps_results_dict(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "pat-xyz")
    monkeypatch.delenv("PLANE_INTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("PLANE_BASE_URL", "https://pm.example")

    monkeypatch.setattr(
        ws_mod.requests,
        "get",
        lambda *a, **k: _FakeResponse({"results": [{"id": "9", "name": "Z", "slug": "z"}]}),
    )

    assert _fetch_workspaces() == [{"id": "9", "name": "Z", "slug": "z"}]
