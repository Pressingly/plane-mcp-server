"""Server instructions for the Moneta fork.

Upstream's instructions are an epic recipe built on ``workitem_type`` and PQL,
neither of which the self-hosted community edition serves, so they would send
the model to a hidden tool and a rejected argument. These replace them.
"""

from __future__ import annotations

MONETA_INSTRUCTIONS = """
Manages projects, work items, cycles, modules, labels, states and members in Plane,
a project management tool. Each tool covers one Plane resource and takes an `action`
argument naming the operation; every available tool is listed up front.

Getting started: call `list_workspaces` to discover workspace slugs, then
`project list` to find projects. Pass `workspace_slug` on each tool call when
working in multi-workspace mode.

Work items are listed per project: `workitem list` needs project_id. PQL filtering
is not supported on this server, so page through `workitem list` (order_by, cursor)
and filter the returned items yourself, or use `workitem search` for text.

Replies leave out fields that have no value. Work item lists (`workitem list`,
`workitem list_archived`, `cycle list_workitems`, `module list_workitems`) return a
compact set of fields unless you pass `fields` (comma-separated, e.g.
`fields="id,name,description_html"`) or `expand`; `fields="*"` returns every field.
"""
