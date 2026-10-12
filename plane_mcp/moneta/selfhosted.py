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
* plane-sdk 0.3 lists projects, cycles, modules and workspace members through
  ``*-lite`` routes, and work item relations through ``dependencies/`` and
  ``work-item-relations/``. None of those exist on the self-hosted ``/api/v1``, so
  they are served from the full routes (``projects/``, ``cycles/``, ``modules/``,
  ``members/``, ``relations/``) and reshaped into the models the SDK expects.
  ``members/`` applies no filters, so the member filters run here.
* There is no workspace-wide work-item list and no workspace state catalogue, so an
  unscoped list, or a state call without a project, fails with a message naming
  ``project_id`` instead of an opaque 404.

:class:`SelfHostedPlaneClient` swaps in resource subclasses that normalise those
payloads before validating them into the SDK's own models, so the tool return types
and their output schemas are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastmcp.exceptions import ToolError
from plane import PlaneClient
from plane.api.cycles import Cycles
from plane.api.modules import Modules
from plane.api.projects import Projects
from plane.api.work_items import WorkItems
from plane.api.work_items.base import prepare_work_item_params
from plane.api.work_items.custom_relations import WorkItemCustomRelations
from plane.api.work_items.dependencies import WorkItemDependencies
from plane.api.workspace_states import WorkspaceStates
from plane.api.workspaces import Workspaces
from plane.models.cycles import PaginatedCycleLiteResponse, PaginatedCycleResponse, PaginatedCycleWorkItemResponse
from plane.models.modules import PaginatedModuleLiteResponse, PaginatedModuleWorkItemResponse
from plane.models.projects import PaginatedProjectLiteResponse
from plane.models.query_params import (
    CycleLiteListQueryParams,
    LiteListQueryParams,
    MemberListQueryParams,
    ProjectLiteListQueryParams,
    RetrieveQueryParams,
    WorkItemQueryParams,
)
from plane.models.work_items import (
    CreateWorkItemDependency,
    PaginatedWorkItemResponse,
    WorkItem,
    WorkItemDependencyResponse,
    WorkItemDetail,
    WorkItemWithRelationType,
)
from plane.models.workspaces import PaginatedWorkspaceMemberResponse

RELATIONS_ALWAYS_EXPANDED = ("assignees", "labels")

NO_WORKSPACE_LIST = (
    "This Plane server has no workspace-wide work item list: pass project_id "
    "(`project list` returns the project ids) and list one project at a time."
)

NO_WORKSPACE_STATES = (
    "This Plane server keeps workflow states per project and has no workspace state catalogue: "
    "pass project_id (`project list` returns the project ids)."
)

# The fields UserLiteSerializer sends for each member, which are all a filter can match.
MEMBER_TEXT_FILTERS = ("first_name", "last_name", "email", "display_name")


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


def lite_query(params: LiteListQueryParams | None) -> dict[str, Any] | None:
    """Ordering and paging params for a full list route, from the SDK's lite params.

    ``include_archived`` has no counterpart on the full routes, and the lite cycle
    ``status`` filter is spelled ``cycle_view`` there.
    """
    if params is None:
        return None
    query = params.to_query_params()
    query.pop("include_archived", None)
    if "status" in query:
        query["cycle_view"] = query.pop("status")
    return query or None


def _contains(value: Any, wanted: str) -> bool:
    return isinstance(value, str) and wanted.lower() in value.lower()


def member_matches(member: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """Apply the name and email filters the self-hosted ``members/`` route ignores.

    They match case-insensitively on a substring and combine with AND. The payload
    carries no role slug, activity or bot flag, so those filters are refused before
    a call gets here (:mod:`plane_mcp.moneta.trims`), never treated as a match.
    """
    return all(_contains(member.get(name), filters[name]) for name in MEMBER_TEXT_FILTERS if filters.get(name))


def dependency_groups(response: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Reshape ``relations/`` (``{type: [{issue_id, project_id}]}``) into dependency groups."""
    return {
        relation_type: [
            {"id": entry.get("issue_id"), "project_id": entry.get("project_id"), "relation_type": relation_type}
            for entry in entries or []
            if isinstance(entry, Mapping)
        ]
        for relation_type, entries in response.items()
        if isinstance(entries, list)
    }


class SelfHostedDependencies(WorkItemDependencies):
    """Dependencies served from the self-hosted ``relations/`` route."""

    def list(self, workspace_slug: str, project_id: str, work_item_id: str) -> WorkItemDependencyResponse:
        response = self._get(f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations")
        return WorkItemDependencyResponse.model_validate(dependency_groups(response or {}))

    def create(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        data: CreateWorkItemDependency,
    ) -> list[WorkItemWithRelationType]:
        self._post(
            f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations",
            {"relation_type": data.relation_type, "issues": data.work_item_ids},
        )
        return [
            WorkItemWithRelationType(id=target, project_id=project_id, relation_type=data.relation_type)
            for target in data.work_item_ids
        ]


class SelfHostedCustomRelations(WorkItemCustomRelations):
    """Custom relation definitions are a Plane Cloud feature, so there are none to list."""

    def list(self, workspace_slug: str, project_id: str, work_item_id: str) -> dict[str, list[Any]]:
        return {}


class SelfHostedWorkItems(WorkItems):
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.dependencies = SelfHostedDependencies(config)
        self.custom_relations = SelfHostedCustomRelations(config)

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

    def list_workspace(self, workspace_slug: str, params: WorkItemQueryParams | None = None) -> Any:
        raise ToolError(NO_WORKSPACE_LIST)


class SelfHostedCycles(Cycles):
    def list(
        self, workspace_slug: str, project_id: str, params: Mapping[str, Any] | None = None
    ) -> PaginatedCycleResponse:
        response = self._get(f"{workspace_slug}/projects/{project_id}/cycles", params=params)
        return PaginatedCycleResponse.model_validate(as_page(response))

    def list_lite(
        self, workspace_slug: str, project_id: str, params: CycleLiteListQueryParams | None = None
    ) -> PaginatedCycleLiteResponse:
        response = self._get(f"{workspace_slug}/projects/{project_id}/cycles", params=lite_query(params))
        return PaginatedCycleLiteResponse.model_validate(as_page(response))

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
    def list_lite(
        self, workspace_slug: str, project_id: str, params: LiteListQueryParams | None = None
    ) -> PaginatedModuleLiteResponse:
        response = self._get(f"{workspace_slug}/projects/{project_id}/modules", params=lite_query(params))
        return PaginatedModuleLiteResponse.model_validate(as_page(response))

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


class SelfHostedProjects(Projects):
    def list_lite(
        self, workspace_slug: str, params: ProjectLiteListQueryParams | None = None
    ) -> PaginatedProjectLiteResponse:
        response = self._get(f"{workspace_slug}/projects", params=lite_query(params))
        return PaginatedProjectLiteResponse.model_validate(as_page(response))


class SelfHostedWorkspaces(Workspaces):
    def get_members_lite(
        self, workspace_slug: str, params: MemberListQueryParams | None = None
    ) -> PaginatedWorkspaceMemberResponse:
        response = self._get(f"{workspace_slug}/members")
        filters = params.model_dump(exclude_none=True) if params else {}
        members = [m for m in as_page(response or [])["results"] if member_matches(m, filters)]
        return PaginatedWorkspaceMemberResponse.model_validate(as_page(members))


class SelfHostedWorkspaceStates(WorkspaceStates):
    """The workspace state catalogue is Plane Cloud's; the self-hosted API has no ``states/`` at workspace level."""

    def _unavailable(self, *args: Any, **kwargs: Any) -> Any:
        raise ToolError(NO_WORKSPACE_STATES)

    list = retrieve = retrieve_by_external_id = create = update = delete = _unavailable


class SelfHostedPlaneClient(PlaneClient):
    """``PlaneClient`` whose resources speak the self-hosted Plane API's routes and response shapes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.work_items = SelfHostedWorkItems(self.config)
        self.cycles = SelfHostedCycles(self.config)
        self.modules = SelfHostedModules(self.config)
        self.projects = SelfHostedProjects(self.config)
        self.workspaces = SelfHostedWorkspaces(self.config)
        self.workspace_states = SelfHostedWorkspaceStates(self.config)
