"""Moneta fork — relates_to and duplicate relations on the self-hosted server.

Upstream v0.3.4 narrowed ``workitem_relation create`` to six dependency types.
The self-hosted ``relations/`` route also accepts ``relates_to`` and ``duplicate``
(``IssueRelationCreateSerializer.RELATION_TYPE_CHOICES``), so the fork routes those
straight to it and advertises all eight.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from requests import Response

import plane_mcp.client as client_module
import plane_mcp.tools.workitem_relation as relation_tools
from plane_mcp.client import PlaneClientContext
from plane_mcp.moneta.relations import (
    RELATION_TYPES,
    RESTORED_TYPES,
    SELF_HOSTED_DELETE_TAIL,
    SELF_HOSTED_FOOTER,
    UPSTREAM_DELETE_TAIL,
)
from plane_mcp.moneta.selfhosted import SelfHostedPlaneClient, remove_relation_url
from plane_mcp.server import get_stdio_mcp

PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
OTHER = "66666666-6666-6666-6666-666666666666"

# IssueRelationCreateSerializer.RELATION_TYPE_CHOICES in plane/apps/api/plane/api/serializers/issue.py
SELF_HOSTED_CHOICES = {
    "blocking",
    "blocked_by",
    "duplicate",
    "relates_to",
    "start_before",
    "start_after",
    "finish_before",
    "finish_after",
}


class FakeRelations:
    def __init__(self, get_response: Any = None) -> None:
        self.get_response = get_response
        self.gets: list[tuple[str, Any]] = []
        self.posts: list[tuple[str, Any]] = []

    def get(self, endpoint: str, params: Any = None) -> Any:
        self.gets.append((endpoint, params))
        return self.get_response

    def post(self, endpoint: str, data: Any = None) -> Any:
        self.posts.append((endpoint, data))
        return []


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


def wire(monkeypatch: pytest.MonkeyPatch, fake: FakeRelations) -> None:
    client = SelfHostedPlaneClient(base_url="https://pm.example", api_key="k")
    for resource in (client.work_items.relations, client.work_items.dependencies):
        monkeypatch.setattr(resource, "_get", fake.get)
        monkeypatch.setattr(resource, "_post", fake.post)
    context = PlaneClientContext(client=client, workspace_slug="acme")
    for module in (client_module, relation_tools):
        monkeypatch.setattr(module, "get_plane_client_context", lambda: context)


def listed(server: FastMCP) -> Any:
    async def run():
        async with Client(server) as c:
            return {t.name: t for t in await c.list_tools()}["workitem_relation"]

    return asyncio.run(run())


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async def run():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    result = asyncio.run(run())
    content = result.structured_content
    if content is None:
        return json.loads(result.content[0].text) if result.content else None
    return content.get("result", content) if isinstance(content, dict) else content


def test_advertised_types_match_the_self_hosted_serializer():
    assert set(RELATION_TYPES) == SELF_HOSTED_CHOICES
    assert RESTORED_TYPES == {"relates_to", "duplicate"}


def test_relation_type_enum_and_description_name_every_type(server):
    tool = listed(server)
    assert set(tool.inputSchema["properties"]["relation_type"]["enum"]) == SELF_HOSTED_CHOICES
    assert SELF_HOSTED_FOOTER in tool.description
    assert "list_definitions" not in tool.description
    create_line = next(line for line in tool.description.split("\n") if line.startswith("  create ("))
    assert create_line.endswith("-- pass relation_type;")
    assert "relation_definition" not in tool.description
    assert set(tool.inputSchema["properties"]["action"]["enum"]) == {"list", "create", "delete"}
    assert "is_dependency" not in tool.inputSchema["properties"]
    assert "is_dependency" not in tool.description
    delete_line = next(line for line in tool.description.split("\n") if line.startswith("  delete ("))
    assert delete_line == f"  delete (project_id, workitem_id, related_workitem_id{SELF_HOSTED_DELETE_TAIL}."
    assert "workspace_slug" in tool.inputSchema["properties"]


@pytest.mark.parametrize("relation_type", sorted(RESTORED_TYPES))
def test_restored_types_are_created_on_the_relations_route(server, monkeypatch, relation_type):
    fake = FakeRelations()
    wire(monkeypatch, fake)

    result = call(
        server,
        "workitem_relation",
        {
            "action": "create",
            "project_id": PROJECT,
            "workitem_id": ITEM,
            "workitem_ids": [OTHER],
            "relation_type": relation_type,
        },
    )

    assert fake.posts == [
        (f"acme/projects/{PROJECT}/work-items/{ITEM}/relations", {"relation_type": relation_type, "issues": [OTHER]})
    ]
    assert result == [{"id": OTHER, "project_id": PROJECT, "relation_type": relation_type}]


def test_retired_create_name_takes_relates_to(server, monkeypatch):
    fake = FakeRelations()
    wire(monkeypatch, fake)

    call(
        server,
        "create_work_item_relation",
        {"project_id": PROJECT, "work_item_id": ITEM, "work_item_ids": [OTHER], "relation_type": "relates_to"},
    )

    assert fake.posts[0][1] == {"relation_type": "relates_to", "issues": [OTHER]}


def test_dependency_types_still_go_through_upstream(server, monkeypatch):
    fake = FakeRelations()
    wire(monkeypatch, fake)

    result = call(
        server,
        "workitem_relation",
        {
            "action": "create",
            "project_id": PROJECT,
            "workitem_id": ITEM,
            "workitem_ids": [OTHER],
            "relation_type": "blocked_by",
        },
    )

    assert fake.posts[0][1] == {"relation_type": "blocked_by", "issues": [OTHER]}
    assert result[0]["relation_type"] == "blocked_by"


def test_missing_targets_get_upstreams_error(server, monkeypatch):
    fake = FakeRelations()
    wire(monkeypatch, fake)

    async def run():
        async with Client(server) as c:
            return await c.call_tool(
                "workitem_relation",
                {"action": "create", "project_id": PROJECT, "workitem_id": ITEM, "relation_type": "relates_to"},
            )

    text = asyncio.run(run()).content[0].text
    assert "requires: workitem_ids" in text
    assert fake.posts == []


def test_list_returns_relates_to_and_duplicate(server, monkeypatch):
    fake = FakeRelations(
        {
            "blocking": [],
            "relates_to": [{"issue_id": OTHER, "project_id": PROJECT}],
            "duplicate": [{"issue_id": ITEM, "project_id": PROJECT}],
        }
    )
    wire(monkeypatch, fake)

    result = call(server, "workitem_relation", {"action": "list", "project_id": PROJECT, "workitem_id": ITEM})

    dependencies = result["dependencies"]
    assert dependencies["relates_to"] == [{"id": OTHER, "project_id": PROJECT, "relation_type": "relates_to"}]
    assert dependencies["duplicate"][0]["relation_type"] == "duplicate"


REMOVE_URL = f"https://pm.example/api/workspaces/acme/projects/{PROJECT}/issues/{ITEM}/remove-relation/"
BLOCKED_BY_OTHER = {"blocked_by": [{"issue_id": OTHER, "project_id": PROJECT}], "relates_to": []}


def response(status: int, headers: dict[str, str] | None = None) -> Response:
    reply = Response()
    reply.status_code = status
    reply.headers.update(headers or {})
    reply._content = b""
    return reply


class FakeSession:
    def __init__(self, reply: Response) -> None:
        self.reply = reply
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> Response:
        self.posts.append({"url": url, **kwargs})
        return self.reply


def signed_in_client() -> SelfHostedPlaneClient:
    """The dual-header client the Cognito path builds: id_token Bearer plus a minted key."""
    client = SelfHostedPlaneClient(base_url="https://pm.example", access_token="id-token")
    client.config.api_key = "minted"
    return client


def wire_signed_in(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeRelations,
    reply: Response,
    client: SelfHostedPlaneClient | None = None,
) -> FakeSession:
    """Fake the relation reads and app-route posts of ``client`` (signed in by default)."""
    client = client or signed_in_client()
    session = FakeSession(reply)
    for resource in (client.work_items.dependencies, client.work_items.custom_relations):
        monkeypatch.setattr(resource, "_get", fake.get)
        monkeypatch.setattr(resource, "session", session)
    context = PlaneClientContext(client=client, workspace_slug="acme")
    for module in (client_module, relation_tools):
        monkeypatch.setattr(module, "get_plane_client_context", lambda: context)
    return session


DELETE_CALLS = (
    (
        "workitem_relation",
        {"action": "delete", "project_id": PROJECT, "workitem_id": ITEM, "related_workitem_id": OTHER},
    ),
    (
        "workitem_relation",
        {
            "action": "delete",
            "project_id": PROJECT,
            "workitem_id": ITEM,
            "related_workitem_id": OTHER,
            "is_dependency": True,
        },
    ),
    ("remove_work_item_relation", {"project_id": PROJECT, "work_item_id": ITEM, "related_work_item_id": OTHER}),
)


@pytest.mark.parametrize(("tool", "args"), DELETE_CALLS)
def test_delete_posts_the_pair_to_the_remove_relation_route(server, monkeypatch, tool, args):
    fake = FakeRelations(BLOCKED_BY_OTHER)
    session = wire_signed_in(monkeypatch, fake, response(204))

    assert call(server, tool, args) is None
    assert fake.gets == [(f"acme/projects/{PROJECT}/work-items/{ITEM}/relations", None)]
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == REMOVE_URL
    assert post["json"] == {"related_issue": OTHER}
    assert post["allow_redirects"] is False
    assert post["headers"]["Authorization"] == "Bearer id-token"


def test_delete_finds_the_pair_from_the_other_side(server, monkeypatch):
    fake = FakeRelations({"blocking": [{"issue_id": OTHER, "project_id": PROJECT}]})
    session = wire_signed_in(monkeypatch, fake, response(204))

    call(server, *DELETE_CALLS[0])

    assert session.posts[0]["json"] == {"related_issue": OTHER}


def test_delete_of_an_unrelated_pair_is_refused_before_the_post(server, monkeypatch):
    fake = FakeRelations({"blocked_by": [], "relates_to": [{"issue_id": ITEM, "project_id": PROJECT}]})
    session = wire_signed_in(monkeypatch, fake, response(204))

    with pytest.raises(ToolError, match=f"has no relation with {OTHER}"):
        call(server, *DELETE_CALLS[0])
    assert session.posts == []


def test_delete_on_an_api_key_connection_is_refused_before_any_request(server, monkeypatch):
    fake = FakeRelations(BLOCKED_BY_OTHER)
    session = wire_signed_in(
        monkeypatch, fake, response(204), SelfHostedPlaneClient(base_url="https://pm.example", api_key="pat")
    )

    with pytest.raises(ToolError, match="needs a signed-in"):
        call(server, *DELETE_CALLS[0])
    assert fake.gets == []
    assert session.posts == []


def test_a_redirect_to_sign_in_is_not_reported_as_a_removal(server, monkeypatch):
    fake = FakeRelations(BLOCKED_BY_OTHER)
    wire_signed_in(monkeypatch, fake, response(302, {"location": "https://auth.example/oauth2/start"}))

    with pytest.raises(ToolError, match="needs a signed-in"):
        call(server, *DELETE_CALLS[0])


def test_a_failed_removal_surfaces_the_status(server, monkeypatch):
    wire_signed_in(monkeypatch, FakeRelations(BLOCKED_BY_OTHER), response(403))

    with pytest.raises(ToolError, match="403"):
        call(server, *DELETE_CALLS[0])


def test_remove_url_is_the_app_route_beside_api_v1():
    assert remove_relation_url("https://pm.example/api/v1", "acme", PROJECT, ITEM) == REMOVE_URL
    assert remove_relation_url("https://pm.example/api/v1/", "acme", PROJECT, ITEM) == REMOVE_URL


def test_upstream_delete_note_is_still_the_one_rewritten():
    tool_text = relation_tools.build_description("x", relation_tools.ACTIONS, relation_tools.FOOTER)
    assert UPSTREAM_DELETE_TAIL in tool_text


def test_custom_relation_arguments_are_refused(server, monkeypatch):
    wire(monkeypatch, FakeRelations())

    with pytest.raises(ToolError, match="custom relation definitions are not available"):
        call(
            server,
            "workitem_relation",
            {
                "action": "create",
                "project_id": PROJECT,
                "workitem_id": ITEM,
                "workitem_ids": [OTHER],
                "relation_definition_id": "d",
            },
        )


def test_restored_create_runs_off_the_loop_with_the_call_context(server, monkeypatch):
    """The SDK call runs in a worker thread and still sees the per-call workspace and token."""
    import threading

    from fastmcp.server.dependencies import get_access_token

    import plane_mcp.moneta.workspace as ws

    seen: dict[str, Any] = {}
    loop_thread = threading.get_ident()
    sentinel_token = object()
    monkeypatch.setattr(client_module, "get_access_token", lambda: sentinel_token)

    def context():
        seen["thread"] = threading.get_ident()
        seen["workspace"] = ws.resolve_workspace(None, "")
        seen["token"] = client_module.get_access_token()
        fake = FakeRelations()
        client = SelfHostedPlaneClient(base_url="https://pm.example", api_key="k")
        monkeypatch.setattr(client.work_items.relations, "_post", fake.post)
        return PlaneClientContext(client=client, workspace_slug=seen["workspace"])

    monkeypatch.setattr(client_module, "get_plane_client_context", context)
    assert get_access_token  # the real dependency is replaced above; imported to document what is checked

    call(
        server,
        "workitem_relation",
        {
            "action": "create",
            "project_id": PROJECT,
            "workitem_id": ITEM,
            "workitem_ids": [OTHER],
            "relation_type": "relates_to",
            "workspace_slug": "other-ws",
        },
    )

    assert seen["workspace"] == "other-ws"
    assert seen["token"] is sentinel_token
    assert seen["thread"] != loop_thread
