"""Moneta fork — plane-sdk adapted to the self-hosted Plane API's routes and response shapes.

Fixtures mirror what ``plane/apps/api`` actually returns: ``IssueSerializer`` sends
bare UUID strings for unexpanded ``assignees``/``labels``, ``expand=parent`` on a
parentless item yields ``{}``, ``cycle_view=current`` returns a bare list,
``members/`` returns a bare list, and ``relations/`` groups ``{issue_id,
project_id}`` pairs by type. plane-sdk 0.3 asks for ``*-lite``, ``dependencies/``
and ``work-item-relations/`` routes the self-hosted API does not have.

Tools are driven through an in-process FastMCP ``Client`` on the real server, so
the curated surface, the injected ``workspace_slug`` and the middleware run too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from plane.models.query_params import CycleLiteListQueryParams, ProjectLiteListQueryParams, RetrieveQueryParams

import plane_mcp.tools.cycle as cycle_tools
import plane_mcp.tools.member as member_tools
import plane_mcp.tools.module as module_tools
import plane_mcp.tools.project as project_tools
import plane_mcp.tools.workitem as workitem_tools
import plane_mcp.tools.workitem_relation as relation_tools
from plane_mcp.client import PlaneClientContext
from plane_mcp.moneta.selfhosted import (
    NO_WORKSPACE_LIST,
    SelfHostedPlaneClient,
    as_page,
    collapse_expanded_fields,
    dependency_groups,
    lite_query,
    member_matches,
    normalize_work_item_detail,
    with_relations_expanded,
)
from plane_mcp.server import get_stdio_mcp

PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
LABEL = "44444444-4444-4444-4444-444444444444"
NEW_LABEL = "55555555-5555-5555-5555-555555555555"
OTHER = "66666666-6666-6666-6666-666666666666"

TOOL_MODULES = (workitem_tools, cycle_tools, module_tools, project_tools, member_tools, relation_tools)


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
        self.posts: list[tuple[str, Any]] = []

    def get(self, endpoint: str, params: Any = None) -> Any:
        self.gets.append((endpoint, params))
        return self.get_response

    def patch(self, endpoint: str, data: Any = None) -> Any:
        self.patches.append((endpoint, data))
        return self.patch_response

    def post(self, endpoint: str, data: Any = None) -> Any:
        self.posts.append((endpoint, data))
        return None


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    return get_stdio_mcp()


def wire(monkeypatch: pytest.MonkeyPatch, fake: FakePlane) -> SelfHostedPlaneClient:
    client = SelfHostedPlaneClient(base_url="https://pm.example", api_key="k")
    resources = (
        client.work_items,
        client.work_items.dependencies,
        client.cycles,
        client.modules,
        client.projects,
        client.workspaces,
    )
    for resource in resources:
        monkeypatch.setattr(resource, "_get", fake.get)
        monkeypatch.setattr(resource, "_patch", fake.patch)
        monkeypatch.setattr(resource, "_post", fake.post)
    context = PlaneClientContext(client=client, workspace_slug="acme")
    for module in TOOL_MODULES:
        monkeypatch.setattr(module, "get_plane_client_context", lambda: context)
    return client


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    """A tool call's structured result, unwrapped from the envelope a union return type gets."""

    async def run():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    content = asyncio.run(run()).structured_content
    return content.get("result", content) if isinstance(content, dict) else content


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


def test_expanded_null_foreign_key_without_an_id_keeps_no_detail():
    estimate = {"key": None, "value": "", "description": ""}
    normalized = normalize_work_item_detail(fork_item(estimate_point=estimate))
    assert normalized["estimate_point"] is None
    assert "estimate_point_detail" not in normalized


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


# --- self-hosted route adapters ----------------------------------------------


def test_lite_query_drops_include_archived_and_renames_status():
    assert lite_query(ProjectLiteListQueryParams(per_page=50, include_archived=False)) == {"per_page": 50}
    assert lite_query(CycleLiteListQueryParams(status="current")) == {"cycle_view": "current"}
    assert lite_query(None) is None


def test_member_filters_match_substrings_and_flags():
    member = {"first_name": "Usama", "email": "usama.sadiq@example.com", "is_bot": False}
    assert member_matches(member, {"first_name": "usa", "email": "SADIQ"})
    assert not member_matches(member, {"display_name": "usama"})
    assert not member_matches(member, {"is_bot": True})
    assert member_matches(member, {"is_active": True})


def test_dependency_groups_reshape_relation_pairs():
    groups = dependency_groups({"blocking": [{"issue_id": OTHER, "project_id": PROJECT}], "duplicate": []})
    assert groups == {
        "blocking": [{"id": OTHER, "project_id": PROJECT, "relation_type": "blocking"}],
        "duplicate": [],
    }


# --- tools, end to end through the MCP client ---------------------------------


def test_retrieve_work_item_accepts_bare_uuid_relations(server, monkeypatch):
    fake = FakePlane(fork_item(parent={}))
    wire(monkeypatch, fake)

    result = call(server, "workitem", {"action": "retrieve", "project_id": PROJECT, "workitem_id": ITEM})

    assert result["assignees"][0]["id"] == USER
    assert result["labels"][0]["id"] == LABEL
    assert result["parent"] is None
    endpoint, params = fake.gets[0]
    assert endpoint == f"acme/projects/{PROJECT}/work-items/{ITEM}"
    assert params["expand"] == "assignees,labels"


def test_retrieve_work_item_by_identifier_keeps_expanded_objects(server, monkeypatch):
    fake = FakePlane(fork_item(assignees=[EXPANDED_ASSIGNEE], labels=[EXPANDED_LABEL]))
    wire(monkeypatch, fake)

    result = call(
        server,
        "workitem",
        {"action": "retrieve_by_identifier", "workitem_identifier": "FOSS-7", "expand": "state"},
    )

    assert result["assignees"][0]["display_name"] == "usama"
    assert result["labels"][0]["name"] == "bug"
    endpoint, params = fake.gets[0]
    assert endpoint == "acme/work-items/FOSS-7"
    assert params["expand"] == "state,assignees,labels"


def test_manage_label_sends_the_merged_update(server, monkeypatch):
    fake = FakePlane(fork_item(), patch_response=fork_item(labels=[LABEL, NEW_LABEL]))
    wire(monkeypatch, fake)

    result = call(
        server,
        "workitem",
        {"action": "manage_label", "project_id": PROJECT, "workitem_id": ITEM, "add_label_id": NEW_LABEL},
    )

    assert fake.patches == [(f"acme/projects/{PROJECT}/work-items/{ITEM}", {"labels": [LABEL, NEW_LABEL]})]
    assert result["id"] == ITEM


def test_retired_remove_assignee_name_sends_the_update(server, monkeypatch):
    fake = FakePlane(fork_item(), patch_response=fork_item(assignees=[]))
    wire(monkeypatch, fake)

    call(server, "manage_work_item_assignee", {"project_id": PROJECT, "work_item_id": ITEM, "remove_user_id": USER})

    assert fake.patches == [(f"acme/projects/{PROJECT}/work-items/{ITEM}", {"assignees": []})]


def fork_page(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "results": list(items),
        "count": len(items),
        "total_count": len(items),
        "total_results": len(items),
        "total_pages": 1,
        "next_cursor": "25:1:0",
        "prev_cursor": "25:-1:1",
        "next_page_results": False,
        "prev_page_results": False,
    }


EXPANDED_NULL_FOREIGN_KEYS = {
    "parent": {},
    "created_by": {},
    "updated_by": {},
    "workspace": {},
    "estimate_point": {"key": None, "value": "", "description": ""},
}


def test_list_work_items_tolerates_expanded_parent(server, monkeypatch):
    wire(monkeypatch, FakePlane(fork_page(fork_item(parent={}))))

    result = call(server, "workitem", {"action": "list", "project_id": PROJECT, "expand": "parent"})

    assert result["results"][0]["parent"] is None
    assert result["next_cursor"] == "25:1:0"


def test_list_work_items_by_external_id_returns_the_single_match(server, monkeypatch):
    wire(monkeypatch, FakePlane(fork_item()))

    result = call(
        server,
        "workitem",
        {"action": "list", "project_id": PROJECT, "external_id": "JIRA-1", "external_source": "jira"},
    )

    assert [item["id"] for item in result["results"]] == [ITEM]


def test_unscoped_work_item_list_names_project_id(server, monkeypatch):
    fake = FakePlane(fork_page())
    wire(monkeypatch, fake)

    with pytest.raises(ToolError, match="pass project_id"):
        call(server, "workitem", {"action": "list"})
    assert fake.gets == []
    assert "project_id" in NO_WORKSPACE_LIST


def test_list_projects_uses_the_full_route(server, monkeypatch):
    fake = FakePlane(fork_page({"id": PROJECT, "identifier": "FOSS", "name": "FOSS", "network": 2}))
    wire(monkeypatch, fake)

    result = call(server, "project", {"action": "list"})

    assert [project["identifier"] for project in result["results"]] == ["FOSS"]
    assert fake.gets[0][0] == "acme/projects"
    assert "include_archived" not in (fake.gets[0][1] or {})


def test_list_cycles_accepts_a_bare_list_from_the_full_route(server, monkeypatch):
    fake = FakePlane(
        [{"id": "cycle-uuid", "name": "Sprint 42", "project": PROJECT, "workspace": "ws", "owned_by": USER}]
    )
    wire(monkeypatch, fake)

    result = call(server, "cycle", {"action": "list", "project_id": PROJECT, "status": "current"})

    assert [cycle["name"] for cycle in result["results"]] == ["Sprint 42"]
    endpoint, params = fake.gets[0]
    assert endpoint == f"acme/projects/{PROJECT}/cycles"
    assert params["cycle_view"] == "current"


def test_list_modules_uses_the_full_route(server, monkeypatch):
    fake = FakePlane(fork_page({"id": "module-uuid", "name": "Auth", "project": PROJECT, "workspace": "ws"}))
    wire(monkeypatch, fake)

    result = call(server, "module", {"action": "list", "project_id": PROJECT})

    assert [module["name"] for module in result["results"]] == ["Auth"]
    assert fake.gets[0][0] == f"acme/projects/{PROJECT}/modules"


def test_workspace_members_are_filtered_here(server, monkeypatch):
    members = [
        {"id": USER, "first_name": "Usama", "display_name": "usama", "role": 20},
        {"id": OTHER, "first_name": "Hieu", "display_name": "hieu", "role": 15},
    ]
    fake = FakePlane(members)
    wire(monkeypatch, fake)

    result = call(server, "member", {"action": "list_workspace", "first_name": "usa"})

    assert [member["id"] for member in result["results"]] == [USER]
    assert fake.gets == [("acme/members", None)]


def test_relations_are_listed_from_the_relations_route(server, monkeypatch):
    fake = FakePlane({"blocked_by": [{"issue_id": OTHER, "project_id": PROJECT}], "relates_to": []})
    wire(monkeypatch, fake)

    result = call(server, "workitem_relation", {"action": "list", "project_id": PROJECT, "workitem_id": ITEM})

    assert result["dependencies"]["blocked_by"][0]["id"] == OTHER
    assert result["custom"] == {}
    assert fake.gets == [(f"acme/projects/{PROJECT}/work-items/{ITEM}/relations", None)]


def test_relations_are_created_on_the_relations_route(server, monkeypatch):
    fake = FakePlane(None)
    wire(monkeypatch, fake)

    call(
        server,
        "workitem_relation",
        {
            "action": "create",
            "project_id": PROJECT,
            "workitem_id": ITEM,
            "workitem_ids": [OTHER],
            "relation_type": "blocking",
        },
    )

    assert fake.posts == [
        (f"acme/projects/{PROJECT}/work-items/{ITEM}/relations", {"relation_type": "blocking", "issues": [OTHER]})
    ]


def test_retrieve_with_expanded_parent_round_trips_through_the_client(server, monkeypatch):
    parent = {"id": "parent-uuid", "sequence_id": 3, "project_id": PROJECT}
    wire(monkeypatch, FakePlane(fork_item(parent=parent)))

    result = call(
        server, "workitem", {"action": "retrieve", "project_id": PROJECT, "workitem_id": ITEM, "expand": "parent"}
    )

    assert result["parent"] == "parent-uuid"
    assert result["parent_detail"] == parent


@pytest.mark.parametrize(
    ("tool", "container_arg", "endpoint"),
    [
        ("cycle", {"cycle_id": "cycle-uuid"}, "cycles/cycle-uuid/cycle-issues"),
        ("module", {"module_id": "module-uuid"}, "modules/module-uuid/module-issues"),
    ],
)
def test_container_work_item_lists_tolerate_expanded_null_foreign_keys(
    server, monkeypatch, tool, container_arg, endpoint
):
    fake = FakePlane(fork_page(fork_item(**EXPANDED_NULL_FOREIGN_KEYS)))
    wire(monkeypatch, fake)
    expand = ",".join(EXPANDED_NULL_FOREIGN_KEYS)

    result = call(server, tool, {"action": "list_workitems", "project_id": PROJECT, **container_arg, "expand": expand})

    item = result["results"][0]
    assert {field: item[field] for field in EXPANDED_NULL_FOREIGN_KEYS} == dict.fromkeys(EXPANDED_NULL_FOREIGN_KEYS)
    assert not any(key.endswith("_detail") for key in item)
    assert fake.gets[0][0] == f"acme/projects/{PROJECT}/{endpoint}"
    assert fake.gets[0][1]["expand"] == expand
