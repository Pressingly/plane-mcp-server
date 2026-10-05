"""Adapt plane-sdk to the response shapes the self-hosted Plane API returns (Moneta fork).

plane-sdk's models are written against Plane Cloud. Our self-hosted Plane
(``plane/apps/api``) answers the same routes with shapes those models reject:

* ``IssueSerializer.to_representation`` returns ``assignees`` and ``labels`` as bare
  UUID strings unless they are in ``expand``, but ``WorkItemDetail`` declares them as
  ``list[UserLite]`` / ``list[Label]``. Every ``retrieve_work_item`` without that
  expand failed validation, and so did ``add/remove_work_item_label/assignee``, which
  retrieve before they update — the update was never sent (FOSS-224).
* ``BaseSerializer`` expands a foreign key into a nested object (``parent`` becomes
  ``{}`` when unset), while the SDK types those fields ``str | None``.
  The cycle-issue and module-issue lists serve work items through the same
  serializer, so they get the same treatment.
* ``cycle_view=current`` on the cycle list returns a bare list, and
  ``external_id`` + ``external_source`` on the work-item list returns a single
  object; the SDK expects a paginated envelope for both.

:class:`SelfHostedPlaneClient` swaps in resource subclasses that normalise those
payloads before validating them into the SDK's own models, so the tool return types
and their output schemas are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plane import PlaneClient
from plane.api.cycles import Cycles
from plane.api.modules import Modules
from plane.api.work_items import WorkItems
from plane.api.work_items.base import prepare_work_item_params
from plane.models.cycles import PaginatedCycleResponse, PaginatedCycleWorkItemResponse
from plane.models.modules import PaginatedModuleWorkItemResponse
from plane.models.query_params import RetrieveQueryParams, WorkItemQueryParams
from plane.models.work_items import PaginatedWorkItemResponse, WorkItem, WorkItemDetail

RELATIONS_ALWAYS_EXPANDED = ("assignees", "labels")


def _id_typed_fields(model: type) -> frozenset[str]:
    return frozenset(name for name, field in model.model_fields.items() if field.annotation == (str | None))


_WORK_ITEM_ID_FIELDS = _id_typed_fields(WorkItem)
_WORK_ITEM_DETAIL_ID_FIELDS = _id_typed_fields(WorkItemDetail)


def collapse_expanded_fields(payload: Mapping[str, Any], id_fields: frozenset[str]) -> dict[str, Any]:
    """Reduce expanded objects on ``str``-typed fields to their id, keeping the object as ``<field>_detail``.

    A null foreign key can still expand to a non-empty object without an id (a
    serializer with writable fields renders its defaults), so ``_detail`` is only
    kept when the object identifies something.
    """
    collapsed: dict[str, Any] = {}
    for key, value in payload.items():
        if key in id_fields and isinstance(value, Mapping):
            collapsed[key] = value.get("id")
            if value.get("id"):
                collapsed[f"{key}_detail"] = dict(value)
        else:
            collapsed[key] = value
    return collapsed


def _as_reference(value: Any, **defaults: Any) -> Any:
    return {"id": value, **defaults} if isinstance(value, str) else value


def normalize_work_item_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a self-hosted work-item payload so it validates as ``WorkItemDetail``."""
    normalized = collapse_expanded_fields(payload, _WORK_ITEM_DETAIL_ID_FIELDS)
    normalized["assignees"] = [_as_reference(a) for a in normalized.get("assignees") or []]
    normalized["labels"] = [_as_reference(lb, name="") for lb in normalized.get("labels") or []]
    return normalized


def as_page(response: Any) -> Any:
    """Wrap a bare list or a single object in the paginated envelope the SDK expects."""
    if isinstance(response, list):
        results = response
    elif isinstance(response, Mapping) and "results" not in response:
        results = [response]
    else:
        return response
    count = len(results)
    return {
        "results": results,
        "count": count,
        "total_count": count,
        "total_results": count,
        "total_pages": 1,
        "next_cursor": "",
        "prev_cursor": "",
        "next_page_results": False,
        "prev_page_results": False,
    }


def normalize_work_item_page(response: Any) -> Any:
    page = as_page(response)
    return {
        **page,
        "results": [collapse_expanded_fields(item, _WORK_ITEM_ID_FIELDS) for item in page.get("results") or []],
    }


def with_relations_expanded(params: RetrieveQueryParams | None) -> dict[str, Any]:
    """Query params for a retrieve, with ``assignees`` and ``labels`` always expanded."""
    query = params.model_dump(exclude_none=True) if params else {}
    requested = [name.strip() for name in query.get("expand", "").split(",") if name.strip()]
    return {**query, "expand": ",".join(dict.fromkeys([*requested, *RELATIONS_ALWAYS_EXPANDED]))}


class SelfHostedWorkItems(WorkItems):
    def retrieve(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        params: RetrieveQueryParams | None = None,
    ) -> WorkItemDetail:
        response = self._get(
            f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}",
            params=with_relations_expanded(params),
        )
        return WorkItemDetail.model_validate(normalize_work_item_detail(response))

    def retrieve_by_identifier(
        self,
        workspace_slug: str,
        project_identifier: str,
        issue_identifier: int,
        params: RetrieveQueryParams | None = None,
    ) -> WorkItemDetail:
        response = self._get(
            f"{workspace_slug}/work-items/{project_identifier}-{issue_identifier}",
            params=with_relations_expanded(params),
        )
        return WorkItemDetail.model_validate(normalize_work_item_detail(response))

    def list(
        self,
        workspace_slug: str,
        project_id: str,
        params: WorkItemQueryParams | None = None,
    ) -> PaginatedWorkItemResponse:
        response = self._get(
            f"{workspace_slug}/projects/{project_id}/work-items",
            params=prepare_work_item_params(params),
        )
        return PaginatedWorkItemResponse.model_validate(normalize_work_item_page(response))


class SelfHostedCycles(Cycles):
    def list(
        self, workspace_slug: str, project_id: str, params: Mapping[str, Any] | None = None
    ) -> PaginatedCycleResponse:
        response = self._get(f"{workspace_slug}/projects/{project_id}/cycles", params=params)
        return PaginatedCycleResponse.model_validate(as_page(response))

    def list_work_items(
        self,
        workspace_slug: str,
        project_id: str,
        cycle_id: str,
        params: WorkItemQueryParams | Mapping[str, Any] | None = None,
    ) -> PaginatedCycleWorkItemResponse:
        response = self._get(
            f"{workspace_slug}/projects/{project_id}/cycles/{cycle_id}/cycle-issues",
            params=prepare_work_item_params(params),
        )
        return PaginatedCycleWorkItemResponse.model_validate(normalize_work_item_page(response))


class SelfHostedModules(Modules):
    def list_work_items(
        self,
        workspace_slug: str,
        project_id: str,
        module_id: str,
        params: WorkItemQueryParams | Mapping[str, Any] | None = None,
    ) -> PaginatedModuleWorkItemResponse:
        response = self._get(
            f"{workspace_slug}/projects/{project_id}/modules/{module_id}/module-issues",
            params=prepare_work_item_params(params),
        )
        return PaginatedModuleWorkItemResponse.model_validate(normalize_work_item_page(response))


class SelfHostedPlaneClient(PlaneClient):
    """``PlaneClient`` whose work-item, cycle and module resources accept self-hosted response shapes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.work_items = SelfHostedWorkItems(self.config)
        self.cycles = SelfHostedCycles(self.config)
        self.modules = SelfHostedModules(self.config)
