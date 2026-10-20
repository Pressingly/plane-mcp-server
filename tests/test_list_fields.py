"""Moneta fork: work item lists default to a compact field set unless the caller asks for more."""

from __future__ import annotations

import pytest

from plane_mcp.moneta.list_fields import COMPACT_LISTS, COMPACT_WORK_ITEM_FIELDS, with_default_fields

LISTS = sorted(COMPACT_LISTS)


@pytest.mark.parametrize("operation", LISTS)
def test_a_list_call_without_fields_gets_the_compact_set(operation):
    assert with_default_fields(operation, {"project_id": "p"})["fields"] == COMPACT_WORK_ITEM_FIELDS


@pytest.mark.parametrize("operation", LISTS)
def test_fields_the_caller_named_are_kept(operation):
    assert with_default_fields(operation, {"fields": "id,description_html"})["fields"] == "id,description_html"


@pytest.mark.parametrize("operation", LISTS)
def test_expand_suppresses_the_default(operation):
    assert "fields" not in with_default_fields(operation, {"expand": "assignees"})


@pytest.mark.parametrize("operation", LISTS)
def test_star_asks_for_every_field(operation):
    assert "fields" not in with_default_fields(operation, {"fields": "*", "project_id": "p"})


@pytest.mark.parametrize(
    "operation", [("workitem", "retrieve"), ("project", "list"), ("workitem", "search"), ("x", None)]
)
def test_other_operations_are_left_alone(operation):
    assert with_default_fields(operation, {"project_id": "p"}) == {"project_id": "p"}


def test_the_arguments_are_not_mutated():
    arguments = {"project_id": "p"}
    with_default_fields(("workitem", "list"), arguments)
    assert arguments == {"project_id": "p"}


def test_the_compact_set_is_made_of_real_work_item_fields():
    from plane.models.work_items import WorkItem

    known = set(WorkItem.model_fields) | {"assignees", "labels"}
    assert set(COMPACT_WORK_ITEM_FIELDS.split(",")) <= known


def test_an_unhashable_action_is_not_an_operation():
    from plane_mcp.moneta.compaction import CompactResponses

    assert CompactResponses()._operation("workitem", {"action": ["list"]}) == ("workitem", None)
