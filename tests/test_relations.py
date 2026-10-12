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

import plane_mcp.client as client_module
import plane_mcp.tools.workitem_relation as relation_tools
from plane_mcp.client import PlaneClientContext
from plane_mcp.moneta.relations import RELATION_TYPES, RESTORED_TYPES, SELF_HOSTED_FOOTER
from plane_mcp.moneta.selfhosted import SelfHostedPlaneClient
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
        return json.loads(result.content[0].text)
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
