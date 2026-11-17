# Changelog

The Moneta fork's changes to `makeplane/plane-mcp-server`, by release tag. Upstream changes pulled in
with a sync are listed only when they change what a client sees.

Tags follow the release pipeline: `vYY.MM.N-rc.N` builds the sandbox image, `vYY.MM.N` the prod image.
Entries start at the release after `v26.11.0`; earlier tags are not back-filled.

Every PR into `foss-sandbox` adds its entry under **Unreleased**. When a tag is cut, rename that
heading to the tag and date and start a new **Unreleased** section. Put anything a client, gateway
policy or operator has to act on under **Behaviour changes**.

## Unreleased

### Added

- `workitem_relation delete` removes a relation between two work items, from either side and whatever
  its type, through Plane's web-app route `issues/<id>/remove-relation/` (the public v1 API has no
  remove route). Cognito connections only; API-key connections are refused with a reason.
  Where two items share more than one relation, each call removes the most recently created one.
  ([FOSS-495](https://pm.moneta.askii.ai/moneta/browse/FOSS-495/), #38)

### Behaviour changes

- `workitem_relation` now advertises `readOnlyHint=false, destructiveHint=true` (it was add-only).
  The hint is per tool, so a read-only or add-only gateway policy now blocks the whole tool,
  `list` and `create` included. The retired names `list_work_item_relations` (read-only) and
  `create_work_item_relation` (add-only) keep their own hints. (#38)
- `workitem_relation`'s `is_dependency` argument is no longer advertised; it is still accepted and
  ignored. (#38)
