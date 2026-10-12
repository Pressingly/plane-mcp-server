"""Read/write label tests for the Moneta fork.

Verifies that every tool the server registers carries an MCP read/write
annotation, that the labels reach a client on ``tools/list``, and that no tool
that changes data is published as a read.

No live Plane backend needed — the labels are set entirely at startup.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from plane_mcp.moneta.annotations import ADDITIVE_WRITE, DESTRUCTIVE_WRITE, READ_ONLY, annotations_for
from plane_mcp.moneta.visibility import _get_all_tools
from plane_mcp.tools import register_tools

# The tools on the curated list that only read. Everything else listed is a write.
EXPECTED_VISIBLE_READS = {
    "get_me",
    "get_project_members",
    "get_workspace_members",
    "list_cycle_work_items",
    "list_cycles",
    "list_labels",
    "list_module_work_items",
    "list_modules",
    "list_projects",
    "list_states",
    "list_work_item_comments",
    "list_work_item_links",
    "list_work_item_relations",
    "list_work_items",
    "list_workspaces",
    "retrieve_cycle",
    "retrieve_label",
    "retrieve_module",
    "retrieve_project",
    "retrieve_state",
    "retrieve_work_item",
    "retrieve_work_item_by_identifier",
    "retrieve_work_item_comment",
    "retrieve_work_item_link",
    "search_work_items",
}


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    """The real tool registry, with the env override cleared."""
    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    mcp = FastMCP("test-annotations")
    register_tools(mcp)
    return mcp


def test_every_registered_tool_is_labelled(server: FastMCP) -> None:
    """Hidden tools are labelled too, so re-activating one needs no second change."""
    unlabelled = [t.name for t in _get_all_tools(server) if t.annotations is None]
    assert unlabelled == []


@pytest.mark.anyio
async def test_listed_reads_are_exactly_the_expected_set(server: FastMCP) -> None:
    async with Client(server) as client:
        listed = await client.list_tools()

    assert all(t.annotations is not None for t in listed)
    reads = {t.name for t in listed if t.annotations.readOnlyHint}
    assert reads == EXPECTED_VISIBLE_READS


@pytest.mark.anyio
async def test_listed_writes_are_not_published_as_reads(server: FastMCP) -> None:
    async with Client(server) as client:
        by_name = {t.name: t.annotations for t in await client.list_tools()}

    assert by_name["create_work_item"].readOnlyHint is False
    assert by_name["create_work_item"].destructiveHint is False
    for name in ("update_work_item", "delete_work_item_comment", "remove_work_item_label", "complete_cycle"):
        assert by_name[name].readOnlyHint is False
        assert by_name[name].destructiveHint is True


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("get_me", READ_ONLY),
        ("list_projects", READ_ONLY),
        ("retrieve_work_item", READ_ONLY),
        ("search_work_items", READ_ONLY),
        ("create_project", ADDITIVE_WRITE),
        ("add_work_item_label", ADDITIVE_WRITE),
        ("attach_page_to_work_item", ADDITIVE_WRITE),
        ("update_project", DESTRUCTIVE_WRITE),
        ("delete_project", DESTRUCTIVE_WRITE),
        ("archive_work_item", DESTRUCTIVE_WRITE),
        ("transfer_cycle_work_items", DESTRUCTIVE_WRITE),
        ("link_estimate_to_project", DESTRUCTIVE_WRITE),
    ],
)
def test_label_follows_the_verb(tool_name: str, expected) -> None:
    assert annotations_for(tool_name) == expected


def test_unknown_verb_is_a_destructive_write() -> None:
    """A tool upstream adds under a new verb is confirmed until it is classified."""
    assert annotations_for("reticulate_splines") == DESTRUCTIVE_WRITE
