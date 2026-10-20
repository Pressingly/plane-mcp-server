"""Moneta fork: the whole server answers compactly, and no value is lost doing it.

The SDK is replaced by a fake that serves recorded payload shapes (``tests/compact_payloads.py``)
and honours ``fields`` the way the API does. "Before" is the same server with the compaction
middleware removed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from plane.models.cycles import PaginatedCycleWorkItemResponse
from plane.models.projects import PaginatedProjectLiteResponse
from plane.models.work_items import PaginatedWorkItemResponse, WorkItemDetail

from plane_mcp.moneta.compaction import CompactResponses
from plane_mcp.moneta.list_fields import COMPACT_WORK_ITEM_FIELDS
from plane_mcp.moneta.serialization import drop_nulls
from plane_mcp.server import get_stdio_mcp
from tests import compact_payloads as payloads

COUNT = 20
IDS = {"workspace_slug": "acme", "project_id": payloads.PROJECT}
WORK_ITEM_ID = "11111111-0000-4000-8000-000000000001"


class FakePlane:
    def __init__(self) -> None:
        self.requested: list[str | None] = []
        self.work_items = SimpleNamespace(list=self._work_items, retrieve=self._retrieve)
        self.cycles = SimpleNamespace(list_work_items=self._cycle_items)
        self.projects = SimpleNamespace(list_lite=self._projects)

    def _page(self, model, make, params) -> Any:
        fields = getattr(params, "fields", None)
        self.requested.append(fields)
        records = [payloads.sparse(make(n), fields) for n in range(1, COUNT + 1)]
        return model.model_validate(payloads.paginated(records))

    def _work_items(self, **kwargs: Any) -> Any:
        return self._page(PaginatedWorkItemResponse, payloads.work_item, kwargs["params"])

    def _cycle_items(self, **kwargs: Any) -> Any:
        return self._page(PaginatedCycleWorkItemResponse, payloads.cycle_item, kwargs["params"])

    def _projects(self, **kwargs: Any) -> Any:
        return self._page(PaginatedProjectLiteResponse, payloads.project_lite, kwargs["params"])

    def _retrieve(self, **kwargs: Any) -> Any:
        fields = getattr(kwargs["params"], "fields", None)
        self.requested.append(fields)
        return WorkItemDetail.model_validate(payloads.sparse(payloads.work_item_detail(1), fields))


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> FakePlane:
    from plane_mcp.tools import cycle, project, workitem

    monkeypatch.delenv("PLANE_MCP_ENABLED_TOOLS", raising=False)
    fake = FakePlane()
    for module in (workitem, cycle, project):
        monkeypatch.setattr(module, "get_plane_client_context", lambda: (fake, "acme"))
    return fake


def _server(compact: bool):
    server = get_stdio_mcp()
    if not compact:
        server.middleware = [m for m in server.middleware if not isinstance(m, CompactResponses)]
    return server


def _call(compact: bool, tool: str, **arguments: Any) -> Any:
    async def run():
        async with Client(_server(compact)) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(run())


def _wire_size(result: Any) -> int:
    text = sum(len(block.text.encode()) for block in result.content)
    structured = len(json.dumps(result.structured_content, separators=(",", ":")).encode())
    return text + structured


def _payload(result: Any) -> Any:
    return json.loads(result.content[0].text)


CALLS = {
    "workitem list": ("workitem", {"action": "list", **IDS}),
    "workitem retrieve": ("workitem", {"action": "retrieve", **IDS, "workitem_id": WORK_ITEM_ID}),
    "cycle list_workitems": ("cycle", {"action": "list_workitems", **IDS, "cycle_id": payloads.CYCLE}),
    "project list": ("project", {"action": "list", "workspace_slug": "acme"}),
}


def test_the_middleware_is_mounted_on_the_stdio_server():
    assert any(isinstance(m, CompactResponses) for m in _server(True).middleware)


@pytest.mark.parametrize("name", ["workitem retrieve", "project list"])
def test_nothing_with_a_value_is_lost(plane, name):
    tool, arguments = CALLS[name]
    before = _call(False, tool, **arguments)
    after = _call(True, tool, **arguments)
    assert _payload(after) == drop_nulls(_payload(before))
    assert after.structured_content == drop_nulls(before.structured_content)
    assert _payload(before) != _payload(after)


def test_a_single_record_keeps_every_field_that_has_a_value(plane):
    tool, arguments = CALLS["workitem retrieve"]
    record = _payload(_call(True, tool, **arguments))
    valued = {name for name, value in payloads.work_item_detail(1).items() if value is not None}
    assert valued <= set(record)


@pytest.mark.parametrize("name", ["workitem list", "cycle list_workitems"])
def test_a_compact_list_equals_the_same_list_with_those_fields_asked_for(plane, name):
    tool, arguments = CALLS[name]
    explicit = _call(False, tool, **arguments, fields=COMPACT_WORK_ITEM_FIELDS)
    defaulted = _call(True, tool, **arguments)
    assert _payload(defaulted) == drop_nulls(_payload(explicit))


@pytest.mark.parametrize("name", ["workitem list", "cycle list_workitems"])
def test_the_default_asks_the_api_for_the_compact_fields_only(plane, name):
    tool, arguments = CALLS[name]
    _call(True, tool, **arguments)
    assert plane.requested == [COMPACT_WORK_ITEM_FIELDS]


@pytest.mark.parametrize("name", ["workitem list", "cycle list_workitems"])
def test_fields_star_returns_every_field_with_a_value(plane, name):
    tool, arguments = CALLS[name]
    full = _call(True, tool, **arguments, fields="*")
    first = _payload(full)["results"][0]
    assert plane.requested == [None]
    assert {"description_html", "created_by", "workspace", "sort_order"} <= set(first)
    assert all(value is not None for value in first.values())


def test_fields_the_caller_names_win_over_the_default(plane):
    tool, arguments = CALLS["workitem list"]
    result = _call(True, tool, **arguments, fields="id,description_html")
    assert plane.requested == ["id,description_html"]
    assert set(_payload(result)["results"][0]) == {"id", "description_html"}


def test_a_list_keeps_its_paging_keys(plane):
    tool, arguments = CALLS["workitem list"]
    page = _payload(_call(True, tool, **arguments))
    assert {"next_cursor", "prev_cursor", "next_page_results", "total_count", "count"} <= set(page)


def test_a_retired_tool_name_gets_the_compact_default_too(plane):
    _call(True, "list_work_items", project_id=payloads.PROJECT, workspace_slug="acme")
    assert plane.requested == [COMPACT_WORK_ITEM_FIELDS]


def test_reply_sizes_shrink(plane, capsys):
    rows = []
    for name, (tool, arguments) in CALLS.items():
        before = _wire_size(_call(False, tool, **arguments))
        after = _wire_size(_call(True, tool, **arguments))
        rows.append((name, before, after, 100 * (before - after) / before))
    with capsys.disabled():
        print("\n| call | before (bytes) | after (bytes) | cut |\n|---|---|---|---|")
        for name, before, after, cut in rows:
            print(f"| {name} | {before} | {after} | {cut:.0f}% |")
    cuts = {name: cut for name, _, _, cut in rows}
    assert cuts["workitem list"] >= 60
    assert cuts["cycle list_workitems"] >= 60
    assert all(cut > 0 for cut in cuts.values())
