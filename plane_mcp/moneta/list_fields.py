"""A compact default field set for the list calls that can take one (Moneta fork).

A work item list without ``fields`` returns every column of every item. These
operations accept a sparse fieldset, so when the caller names neither ``fields``
nor ``expand`` they are given :data:`COMPACT_WORK_ITEM_FIELDS`. ``fields="*"`` asks
for the full record, and any other ``fields`` value is passed through untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COMPACT_WORK_ITEM_FIELDS = (
    "id,name,sequence_id,project,state,priority,assignees,labels,parent,start_date,target_date,completed_at,updated_at"
)
ALL_FIELDS = "*"

COMPACT_LISTS: Mapping[tuple[str, str], str] = {
    ("workitem", "list"): COMPACT_WORK_ITEM_FIELDS,
    ("workitem", "list_archived"): COMPACT_WORK_ITEM_FIELDS,
    ("cycle", "list_workitems"): COMPACT_WORK_ITEM_FIELDS,
    ("module", "list_workitems"): COMPACT_WORK_ITEM_FIELDS,
}


def _asks_for_more(arguments: Mapping[str, Any]) -> bool:
    return bool(arguments.get("fields") or arguments.get("expand"))


def with_default_fields(operation: tuple[str, str | None], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments of a list call with the compact field set applied where it belongs."""
    if arguments.get("fields") == ALL_FIELDS:
        return {name: value for name, value in arguments.items() if name != "fields"}
    default = COMPACT_LISTS.get(operation)
    if default is None or _asks_for_more(arguments):
        return dict(arguments)
    return {**arguments, "fields": default}
