"""Plane API payloads shaped like the self-hosted ``/api/v1`` answers, for the compaction tests.

Recorded shapes, not live captures: no staging access was used. Every key the SDK
models declare is present, with ``null`` where Plane has no value, which is how the
API serialises an unset column.
"""

from __future__ import annotations

from typing import Any

WORKSPACE = "aaaaaaaa-0000-4000-8000-000000000001"
PROJECT = "bbbbbbbb-0000-4000-8000-000000000002"
STATE = "cccccccc-0000-4000-8000-000000000003"
USER = "dddddddd-0000-4000-8000-000000000004"
LABEL = "eeeeeeee-0000-4000-8000-000000000005"
CYCLE = "ffffffff-0000-4000-8000-000000000006"
STAMP = "2026-10-12T09:30:00.123456Z"
BODY = "<p>" + "Acceptance criteria are listed in the ticket and the rollout is staged. " * 4 + "</p>"

ITEM_FIELDS = (
    "id,name,sequence_id,project,state,priority,assignees,labels,parent,start_date,target_date,completed_at,updated_at"
)


def work_item(number: int) -> dict[str, Any]:
    return {
        "id": f"11111111-0000-4000-8000-{number:012d}",
        "type_id": None,
        "created_at": STAMP,
        "updated_at": STAMP,
        "deleted_at": None,
        "point": None,
        "name": f"Work item {number}",
        "description_html": BODY,
        "description_stripped": "Acceptance criteria are listed in the ticket and the rollout is staged. " * 4,
        "description_binary": None,
        "priority": "medium",
        "start_date": None,
        "target_date": "2026-11-01",
        "sequence_id": number,
        "sort_order": 65535.0,
        "completed_at": None,
        "archived_at": None,
        "is_draft": False,
        "external_source": None,
        "external_id": None,
        "created_by": USER,
        "updated_by": None,
        "project": PROJECT,
        "workspace": WORKSPACE,
        "parent": None,
        "state": STATE,
        "estimate_point": None,
        "type": None,
        "assignees": [USER],
        "labels": [LABEL],
    }


def work_item_detail(number: int) -> dict[str, Any]:
    return {**work_item(number), "assignees": [], "labels": [LABEL]}


def sparse(record: dict[str, Any], fields: str | None) -> dict[str, Any]:
    if not fields:
        return record
    wanted = set(fields.split(","))
    return {name: value for name, value in record.items() if name in wanted}


def paginated(records: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "grouped_by": None,
        "sub_grouped_by": None,
        "total_count": len(records),
        "next_cursor": "20:1:0",
        "prev_cursor": "20:-1:1",
        "next_page_results": False,
        "prev_page_results": False,
        "count": len(records),
        "total_pages": 1,
        "total_results": len(records),
        "extra_stats": None,
        "results": records,
        **extra,
    }


def cycle_item(number: int) -> dict[str, Any]:
    return {
        **work_item(number),
        "sub_issues_count": None,
        "link_count": None,
        "attachment_count": None,
        "is_subscribed": None,
    }


def project_lite(number: int) -> dict[str, Any]:
    return {
        "id": f"22222222-0000-4000-8000-{number:012d}",
        "identifier": f"P{number}",
        "name": f"Project {number}",
        "cover_image": None,
        "icon_prop": None,
        "emoji": None,
        "description": "A project",
        "cover_image_url": None,
        "archived_at": None,
    }
