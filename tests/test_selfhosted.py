"""Moneta fork — plane-sdk adapted to the self-hosted Plane API's response shapes.

Fixtures mirror what ``plane/apps/api`` actually returns: ``IssueSerializer`` sends
bare UUID strings for unexpanded ``assignees``/``labels``, ``expand=parent`` on a
parentless item yields ``{}``, and ``cycle_view=current`` returns a bare list.
Tools are driven through an in-process FastMCP ``Client`` so the output schema
derived from the SDK return annotation is exercised too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from plane.models.query_params import RetrieveQueryParams

import plane_mcp.tools.cycles as cycle_tools
import plane_mcp.tools.work_items as work_item_tools
from plane_mcp.client import PlaneClientContext
from plane_mcp.moneta.selfhosted import (
    SelfHostedPlaneClient,
    as_page,
    collapse_expanded_fields,
    normalize_work_item_detail,
    with_relations_expanded,
)
from plane_mcp.tools import register_tools

PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
LABEL = "44444444-4444-4444-4444-444444444444"
NEW_LABEL = "55555555-5555-5555-5555-555555555555"


def fork_item(**overrides: Any) -> dict[str, Any]:
    return {
        "id": ITEM,
        "name": "Fix login",
        "sequence_id": 7,
        "priority": "high",
        "project": PROJECT,
        "workspace": "ws-uuid",
        "state": "state-uuid",
        "parent": None,
        "assignees": [USER],
        "labels": [LABEL],
        **overrides,
    }


EXPANDED_ASSIGNEE = {"id": USER, "display_name": "usama", "first_name": "Usama"}
EXPANDED_LABEL = {"id": LABEL, "name": "bug", "color": "#f00"}


class FakePlane:
    """Records the requests a tool makes and answers with fork-shaped payloads."""

    def __init__(self, get_response: Any, patch_response: Any = None) -> None:
        self.get_response = get_response
        self.patch_response = patch_response
        self.gets: list[tuple[str, Any]] = []
        self.patches: list[tuple[str, Any]] = []

    def get(self, endpoint: str, params: Any = None) -> Any:
        self.gets.append((endpoint, params))
        return self.get_response

    def patch(self, endpoint: str, data: Any = None) -> Any:
        self.patches.append((endpoint, data))
        return self.patch_response


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    mcp = FastMCP("t")
    register_tools(mcp)
    return mcp


def wire(monkeypatch: pytest.MonkeyPatch, fake: FakePlane) -> SelfHostedPlaneClient:
    client = SelfHostedPlaneClient(base_url="https://pm.example", api_key="k")
    for resource in (client.work_items, client.cycles):
        monkeypatch.setattr(resource, "_get", fake.get)
        monkeypatch.setattr(resource, "_patch", fake.patch)
    context = PlaneClientContext(client=client, workspace_slug="acme")
    monkeypatch.setattr(work_item_tools, "get_plane_client_context", lambda: context)
    monkeypatch.setattr(cycle_tools, "get_plane_client_context", lambda: context)
    return client


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async def run():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(run())


# --- pure normalisers ---------------------------------------------------------


def test_bare_relation_ids_become_references():
    normalized = normalize_work_item_detail(fork_item())
    assert normalized["assignees"] == [{"id": USER}]
    assert normalized["labels"] == [{"id": LABEL, "name": ""}]


def test_expanded_relations_are_left_untouched():
    payload = fork_item(assignees=[EXPANDED_ASSIGNEE], labels=[EXPANDED_LABEL])
    normalized = normalize_work_item_detail(payload)
    assert normalized["assignees"] == [EXPANDED_ASSIGNEE]
    assert normalized["labels"] == [EXPANDED_LABEL]


def test_empty_expanded_parent_becomes_none():
    normalized = normalize_work_item_detail(fork_item(parent={}))
    assert normalized["parent"] is None
    assert "parent_detail" not in normalized


def test_expanded_foreign_key_collapses_to_id_and_keeps_the_object():
    parent = {"id": "parent-uuid", "sequence_id": 3, "project_id": PROJECT}
    normalized = normalize_work_item_detail(fork_item(parent=parent))
    assert normalized["parent"] == "parent-uuid"
    assert normalized["parent_detail"] == parent


def test_collapse_ignores_fields_that_already_accept_objects():
    project = {"id": PROJECT, "identifier": "FOSS"}
    assert collapse_expanded_fields({"project": project}, frozenset({"parent"})) == {"project": project}


def test_normalising_does_not_mutate_the_payload():
    payload = fork_item(parent={})
    normalize_work_item_detail(payload)
    assert payload == fork_item(parent={})


def test_retrieve_always_expands_assignees_and_labels_without_duplicates():
    query = with_relations_expanded(RetrieveQueryParams(expand="state, labels", fields="id,name"))
    assert query == {"expand": "state,labels,assignees", "fields": "id,name"}
    assert with_relations_expanded(None) == {"expand": "assignees,labels"}


def test_as_page_wraps_a_bare_list():
    page = as_page([{"name": "Sprint 1"}, {"name": "Sprint 2"}])
    assert page["results"] == [{"name": "Sprint 1"}, {"name": "Sprint 2"}]
    assert page["total_count"] == page["count"] == page["total_results"] == 2
    assert page["next_page_results"] is False


def test_as_page_wraps_a_single_object():
    assert as_page({"id": ITEM})["results"] == [{"id": ITEM}]


def test_as_page_passes_an_envelope_through():
    envelope = {"results": [], "total_count": 0}
    assert as_page(envelope) is envelope


# --- tools, end to end through the MCP client ---------------------------------


def test_retrieve_work_item_accepts_bare_uuid_relations(server, monkeypatch):
    fake = FakePlane(fork_item(parent={}))
    wire(monkeypatch, fake)

    result = call(server, "retrieve_work_item", {"project_id": PROJECT, "work_item_id": ITEM})

    assert result.data is not None
    assert result.structured_content["assignees"][0]["id"] == USER
    assert result.structured_content["labels"][0]["id"] == LABEL
    assert result.structured_content["parent"] is None
    endpoint, params = fake.gets[0]
    assert endpoint == f"acme/projects/{PROJECT}/work-items/{ITEM}"
    assert params["expand"] == "assignees,labels"


def test_retrieve_work_item_by_identifier_keeps_expanded_objects(server, monkeypatch):
    fake = FakePlane(fork_item(assignees=[EXPANDED_ASSIGNEE], labels=[EXPANDED_LABEL]))
    wire(monkeypatch, fake)

    result = call(server, "retrieve_work_item_by_identifier", {"work_item_identifier": "FOSS-7", "expand": "state"})

    assert result.structured_content["assignees"][0]["display_name"] == "usama"
    assert result.structured_content["labels"][0]["name"] == "bug"
    endpoint, params = fake.gets[0]
    assert endpoint == "acme/work-items/FOSS-7"
    assert params["expand"] == "state,assignees,labels"


def test_add_work_item_label_sends_the_update(server, monkeypatch):
    fake = FakePlane(fork_item(), patch_response=fork_item(labels=[LABEL, NEW_LABEL]))
    wire(monkeypatch, fake)

    result = call(server, "add_work_item_label", {"project_id": PROJECT, "work_item_id": ITEM, "label_id": NEW_LABEL})

    assert fake.patches == [(f"acme/projects/{PROJECT}/work-items/{ITEM}", {"labels": [LABEL, NEW_LABEL]})]
    assert result.structured_content["id"] == ITEM


def test_remove_work_item_assignee_sends_the_update(server, monkeypatch):
    fake = FakePlane(fork_item(), patch_response=fork_item(assignees=[]))
    wire(monkeypatch, fake)

    call(server, "remove_work_item_assignee", {"project_id": PROJECT, "work_item_id": ITEM, "user_id": USER})

    assert fake.patches == [(f"acme/projects/{PROJECT}/work-items/{ITEM}", {"assignees": []})]


def test_list_work_items_tolerates_expanded_parent(server, monkeypatch):
    page = {
        "results": [fork_item(parent={})],
        "count": 1,
        "total_count": 1,
        "total_results": 1,
        "total_pages": 1,
        "next_cursor": "25:1:0",
        "prev_cursor": "25:-1:1",
        "next_page_results": False,
        "prev_page_results": False,
    }
    wire(monkeypatch, FakePlane(page))

    result = call(server, "list_work_items", {"project_id": PROJECT, "expand": "parent"})

    assert result.structured_content["results"][0]["parent"] is None
    assert result.structured_content["next_cursor"] == "25:1:0"


def test_list_work_items_by_external_id_returns_the_single_match(server, monkeypatch):
    wire(monkeypatch, FakePlane(fork_item()))

    result = call(
        server, "list_work_items", {"project_id": PROJECT, "external_id": "JIRA-1", "external_source": "jira"}
    )

    assert [item["id"] for item in result.structured_content["results"]] == [ITEM]


def test_list_cycles_accepts_a_bare_list(server, monkeypatch):
    cycles = [{"id": "cycle-uuid", "name": "Sprint 42"}]
    fake = FakePlane(cycles)
    wire(monkeypatch, fake)

    result = call(server, "list_cycles", {"project_id": PROJECT, "params": {"cycle_view": "current"}})

    assert [cycle["name"] for cycle in result.structured_content["result"]] == ["Sprint 42"]
    assert fake.gets == [(f"acme/projects/{PROJECT}/cycles", {"cycle_view": "current"})]


def test_retrieve_with_expanded_parent_round_trips_through_the_client(server, monkeypatch):
    parent = {"id": "parent-uuid", "sequence_id": 3, "project_id": PROJECT}
    wire(monkeypatch, FakePlane(fork_item(parent=parent)))

    result = call(server, "retrieve_work_item", {"project_id": PROJECT, "work_item_id": ITEM, "expand": "parent"})

    assert result.data is not None
    assert result.structured_content["parent"] == "parent-uuid"
    assert result.structured_content["parent_detail"] == parent
