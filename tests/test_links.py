"""Moneta fork: workitem_link refuses link URLs that are not plain http(s) addresses."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import plane_mcp.tools.workitem_link as link_tools
from plane_mcp.moneta.links import is_web_url
from plane_mcp.server import get_stdio_mcp

PROJECT = "11111111-1111-1111-1111-111111111111"

UNSAFE_URLS = [
    "javascript:alert(document.domain)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "\x01javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//evil.example/path",
    "https://",
    "https://example.com/a b",
    "https://example.com/\nnext",
    "https://[::1",
    "https://[evil]/x",
]

WEB_URLS = ["https://example.com", "http://example.com/spec?x=1#y", "HTTPS://Example.com/Path"]

WRITE_CALLS = {
    "create": {"action": "create", "project_id": PROJECT, "workitem_id": "w"},
    "update": {"action": "update", "project_id": PROJECT, "workitem_id": "w", "link_id": "l"},
}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async def run():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(run())


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_is_web_url_rejects_unsafe_urls(url):
    assert not is_web_url(url)


@pytest.mark.parametrize("url", WEB_URLS)
def test_is_web_url_accepts_web_urls(url):
    assert is_web_url(url)


@pytest.mark.parametrize("action", sorted(WRITE_CALLS))
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,hi"])
def test_unsafe_url_is_refused_before_reaching_plane(server, monkeypatch, action, url):
    monkeypatch.setattr(link_tools, "get_plane_client_context", lambda: pytest.fail("reached the client"))

    with pytest.raises(ToolError, match="http:// or https://"):
        call(server, "workitem_link", {**WRITE_CALLS[action], "url": url})


def test_unsafe_url_is_refused_on_a_retired_name_too(server, monkeypatch):
    monkeypatch.setattr(link_tools, "get_plane_client_context", lambda: pytest.fail("reached the client"))

    with pytest.raises(ToolError, match="http:// or https://"):
        call(
            server,
            "update_work_item_link",
            {"project_id": PROJECT, "work_item_id": "w", "link_id": "l", "url": "javascript:alert(1)"},
        )


@pytest.mark.parametrize("action", sorted(WRITE_CALLS))
def test_web_url_passes_through(server, monkeypatch, action):
    def reached():
        raise RuntimeError("reached the client")

    monkeypatch.setattr(link_tools, "get_plane_client_context", reached)

    with pytest.raises(ToolError, match="reached the client"):
        call(server, "workitem_link", {**WRITE_CALLS[action], "url": "https://example.com/spec"})


def test_title_only_update_passes_through(server, monkeypatch):
    def reached():
        raise RuntimeError("reached the client")

    monkeypatch.setattr(link_tools, "get_plane_client_context", reached)

    with pytest.raises(ToolError, match="reached the client"):
        call(server, "workitem_link", {**WRITE_CALLS["update"], "title": "Design spec"})
