# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Plane MCP Server — a Python-based Model Context Protocol server that exposes Plane's project management API as MCP tools. Built on FastMCP with the official `plane-sdk`. Supports three transport modes: stdio (local), HTTP (with OAuth or header auth), and SSE (legacy).

## Common Commands

```bash
# Install dependencies (uses uv)
uv pip install -e ".[dev]"

# Run the server locally (stdio mode)
PLANE_API_KEY=... PLANE_WORKSPACE_SLUG=... python -m plane_mcp stdio

# Run HTTP server
python -m plane_mcp http

# Run all tests
pytest

# Run a single test
pytest tests/test_visibility.py -v

# The write-path E2E test is skipped unless its actions are opted back in
PLANE_MCP_ENABLED_TOOLS=create_project,delete_project,delete_work_item,create_work_item,update_work_item \
  pytest tests/test_integration.py::test_full_integration -v

# Run tests with env vars from file
export $(cat .env.test.local | xargs) && pytest tests/ -v

# Format code (line length: 120)
ruff format plane_mcp/

# Lint (rules: E, F, I, UP, B; line length: 120)
ruff check plane_mcp/
```

## Architecture

### Entry Point & Transport Modes

`plane_mcp/__main__.py` parses a positional arg (`stdio`, `http`, or `sse`) and launches the corresponding server:
- **stdio**: Requires `PLANE_API_KEY` + `PLANE_WORKSPACE_SLUG` env vars. Runs locally.
- **http**: Starts on port 8211 with two auth endpoints — OAuth (`/oauth/mcp`) and header-based PAT (`/http/api-key/mcp`).
- **sse**: Legacy OAuth-only SSE transport.

### Server Factories (`server.py`)

Three factory functions (`get_oauth_mcp`, `get_header_mcp`, `get_stdio_mcp`) each create a `FastMCP` instance and configure the appropriate auth provider, then hand it to `_configured()` for the middleware stack and tools — one place, so a transport cannot be left a middleware behind. OAuth/HTTP modes use Redis for token storage (falls back to in-memory).

### Middleware (`middleware.py`)

Ordered as registered; the earlier one wraps the later:

| Middleware | Does |
|---|---|
| `PlaneLoggingMiddleware` | structured logging, plus the tool name |
| `CoerceArguments` | repairs arguments a client encoded as strings, before validation (`coercion.py`) |
| `ValidateActionArguments` | refuses a call with no `action`, and arguments the chosen action does not accept, from the `ACTIONS` declaration |


A dispatch tool's name is not its operation, so `PlaneLoggingMiddleware` adds `resource` and `action` to every `tools/call` record — start, success and error alike, where previously only success and error carried anything. For a retired name both are resolved through the alias table, since it carries no `action` of its own.

| field | means |
|---|---|
| `tool` | the name the caller used — **unchanged**, so existing dashboards keep counting the same thing |
| `resource` | the resource tool that ran (`workitem`); equals `tool` for a current call |
| `action` | the operation (`count`), absent only for a tool that has no actions |

`resource` + `action` names one operation however it was reached, and `tool != resource` is exactly the set of calls still arriving on a retired name. `legacy.py` also keeps its per-resolution log line, which predates these fields.

Coercion runs before validation so an argument is judged by the value it repairs to. `ValidateActionArguments` closes a gap a per-tool schema cannot: every action's parameters share one schema, so an argument meant for another action validated cleanly and was then dropped, and the call answered a different question than the one asked. Only arguments carrying a value are judged. It also answers a call that names no `action` at all, because the schema error for that case reports the missing parameter without reporting one permitted value — and an agent probing a tool with empty arguments to learn its interface is asking exactly the question the action list answers. Retired names are exempt from both checks: they are keyed by their own retired name, which the `ACTIONS` table does not carry, so nothing there claims them.

### Client Context (`client.py`)

`get_plane_client_context()` returns a `PlaneClientContext(client, workspace_slug)` namedtuple. It resolves credentials from the MCP request context (OAuth token or header API key) or from environment variables (stdio mode). Prefers `PLANE_INTERNAL_BASE_URL` for server-to-server calls.

### Authentication (`auth/`)

- `PlaneOAuthProvider` — Full OAuth flow with token verification against the Plane API.
- `PlaneHeaderAuthProvider` — Simple header-based auth using `x-api-key` and `x-workspace-slug` headers.

### Tools (`tools/`)

One action-dispatch tool per Plane resource: **30 tools, 207 actions, ~67k chars advertised**. `tools/__init__.py` re-exports `register_tools`, so `server.py` and `__main__.py` see a single entry point.

One module per resource, each exporting `NAME`, `ACTIONS`, `LEGACY` and `register(mcp)`. `ACTIONS` is the single source of truth: the tool description and its `ToolAnnotations` are generated from it, and the conformance suite asserts they agree with the function signature. See `tools/README.md` for the full convention.

`tools/` contains resource modules plus `registry.py` (the `RESOURCES` tuple and alias tables) and `legacy.py` (retired-name resolution). Shared helpers live in `plane_mcp/toolkit/`, not here — see below.

Where a resource exists at both project and workspace scope, it resolves that once in a local `_scope_of` rather than through a shared abstraction: the two resources that need it need different shapes (`workitem_type` is a two-way split, `workitem_property` three-way plus a method-name suffix).

`RESOURCES` is an explicit tuple, not a directory scan. Its order is the advertised order and therefore a wire-format guarantee: tool definitions head a client's prompt cache, so reordering invalidates live conversations. Append; never re-sort. `test_resource_order_is_pinned` holds it to a literal list.

**Retired names.** Before consolidation this server exposed 177 tools, one per operation. 169 of those names still resolve, via a `Transform` mapping each to its `(tool, action)` pair with `action` hidden, and keeping the parameter spelling they shipped with (`work_item_id`, not `workitem_id`). The transforms implement `list_tools`/`get_tool` only — execution keeps the full schema, so tool results are unchanged, and nothing is advertised so the listing is unaffected. Seven encoded their action in a parameter and are declared in `LEGACY_UNMAPPED` with a replacement. `tests/tools/_retired_names.py` is the frozen record of all 177.

Tools return Pydantic models from `plane-sdk` and use Python 3.10+ union syntax (`str | None`).

### Toolkit (`toolkit/`)

Shared building blocks for the tool surface, split by *when* they act:

| Module | Acts at | Provides |
|---|---|---|
| `spec.py` | declaration | `Action`, `build_description`, `build_annotations` |
| `runtime.py` | call | `missing`, `needs`, `require`, `one_of`, `opt`, `coerce_list`, `page_params`, `as_params`, `ids_of` |
| `mentions.py` | call | `render_mentions`, `tokenize_mentions`, `mention_user_ids`, `project_mention_error` |
| `paging.py` | response | `envelope`, `dump_results`, `pql_failure`, `workitem_page` |
| `governance.py` | policy | `workspace_owns_resource`, `GOVERNED_BY`, `workspace_owns`, `migration_in_progress`, `plan_gated` |
| `transforms.py` | listing | `StripOutputSchemas` |

Governance, plan gates and the rest of the toolkit conventions are documented in `plane_mcp/toolkit/README.md`, including what adding a newly governed resource takes.

Names are re-exported from `plane_mcp/toolkit/__init__.py`, so a resource module needs one import: `from plane_mcp.toolkit import Action, build_description, missing, opt`.

These sit outside `tools/` deliberately. They were previously `_`-prefixed modules inside the resource package, where the underscore was the module-discovery filter rather than a privacy marker — which made helper filenames load-bearing and made the most widely imported module in the package look private. Nothing here knows which catalogue is calling it.

Anything that encodes the catalogue's history — `LegacyNames`, the `RESOURCES` tuple — belongs under `tools/`, not here.

### Testing

`tests/tools/` covers the surface with no network and no credentials: surface-wide invariants, plus every action of every resource executed against `SpyClient`, a stand-in that binds each call against the genuine SDK signature and type-checks its arguments.

Integration tests in `tests/test_integration.py` use `FastMCP.Client` with `StreamableHttpTransport`. Tests run against a live Plane instance — configure via `.env.test` (copy to `.env.test.local` with real values).

## Key Environment Variables

| Variable | Required For | Purpose |
|---|---|---|
| `PLANE_API_KEY` | stdio | API key for authentication |
| `PLANE_WORKSPACE_SLUG` | stdio | Target workspace |
| `PLANE_BASE_URL` | all (default: https://api.plane.so) | Plane API URL |
| `PLANE_INTERNAL_BASE_URL` | http/sse (optional) | Internal URL for server-to-server calls |
| `REDIS_URL` | http/sse (optional) | Token storage as one connection URL (`redis://`/`rediss://`); wins over host/port |
| `REDIS_HOST` / `REDIS_PORT` | http/sse (optional) | Token storage (falls back to in-memory) |
| `PLANE_OAUTH_PROVIDER_*` | http/sse OAuth | OAuth client credentials and base URL |
| `PLANE_OAUTH_ALLOWED_REDIRECT_URIS` | http/sse OAuth (optional) | Comma-separated redirect URI patterns appended to the built-in allowlist (onboard clients without a release) |
| `LOG_USER_INFO` | all (optional, default: false) | When `true`, include user info (PII such as display name) in logs alongside the opaque user id |
| `LOG_PAYLOADS` | all (optional, default: true) | Log request payloads.|

---

# CLAUDE.md MONETA

This half documents the **Moneta fork** — the changes that make this server run inside the
`foss-server-bundle` devstack behind AWS Cognito / mPass SSO.
Everything above is upstream `makeplane/plane-mcp-server`; everything below is fork-specific.

## Branch & commit policy

This repo follows the same convention as every other app in the bundle:

| Branch | Role |
|---|---|
| `foss-main` | **Production.** The live branch — prod images are built from here. |
| `foss-sandbox` | **Staging.** Everything lands here first and is verified on staging. |
| `main` | Upstream tracking only (e.g. tag `v2.5.0-phoenix-838c572`). Carries **no** Moneta code. |

- Work goes `branch` → PR into `foss-sandbox` → promote to `foss-main`. Never commit directly to
  either, and never target `foss-main` from a feature branch.
- The Moneta integration **is committed**, on `foss-main` and `foss-sandbox`. Do not look for it
  on `main`: `main` has no `plane_mcp/moneta/` package and no Cognito hook in `__main__.py`, so a
  server built from it has no SSO mode, no `list_workspaces`, and no `workspace_slug` argument.
- `jawad/*`, `wip/*`, `*-old` and `revert-*` are dead branches — do not reference or forward-port
  them.
- `foss-server-bundle`'s `make dev.clone.plane-mcp` clones `foss-main`, which is correct.

## Fork-isolation rule (read before editing)

All fork logic lives in the **`plane_mcp/moneta/`** subpackage. Upstream modules carry only
**tiny additive hooks** (one import + one call each), so upstream pulls stay conflict-free. The
resource modules under `plane_mcp/tools/` (and `tools/__init__.py`) are **byte-pristine** and
must stay that way.

**When adding a fork feature: put the logic in `plane_mcp/moneta/` and add a one-line hook. Do
not edit upstream modules in place.** Accept DRY duplication with upstream over merge pain.

Upstream files touched (the entire upstream footprint):

| File | Hook |
|---|---|
| `__main__.py` | http mode → `moneta.http.run()` when `moneta.http.enabled()` (i.e. `COGNITO_USER_POOL_ID` set), else upstream Plane-OAuth+SSE |
| `client.py` | `_workspace_slug` calls `workspace_for(...)`; `get_plane_client_context` calls `bearer_for(...)` and `build_plane_client(...)` |
| `server.py` | `_configured` imports `ValidateActionArguments` from `moneta.middleware` (tolerates the injected `workspace_slug`) and returns `apply_moneta_surface(mcp)` after `register_tools` |

`tools/__init__.py` is untouched: the fork surface is applied in `_configured`, so upstream's
catalogue tests (`tests/tools/`, which call `register_tools` directly) keep testing the full
upstream catalogue, while every transport (and `moneta.http`, which reuses `_configured`) serves
the curated one.

## `plane_mcp/moneta/` modules

| Module | Responsibility |
|---|---|
| `cognito.py` | `PlaneCognitoProvider(AWSCognitoProvider)` — captures the Cognito **id_token** and stashes `{id_token, email, cognito:username}` under the issued JWT's `upstream_claims`. |
| `client.py` | `bearer_for(token, claims)` (forwards the id_token), `plane_request_auth()` (base_url + headers for raw **app-API** calls). |
| `apitoken.py` | Mints/caches a Plane `APIToken` and builds the dual-header `PlaneClient` (see "The `/api/v1` 401 fix"). |
| `selfhosted.py` | `SelfHostedPlaneClient` — normalises the self-hosted API's response shapes (bare UUID `assignees`/`labels`, expanded `parent: {}`, bare-list cycles) and serves plane-sdk 0.3's Cloud-only routes from the self-hosted ones: `*-lite` lists → `projects/`, `cycles/`, `modules/`, `members/` (member filters applied here); `dependencies/` → `relations/`; custom relations → none; the workspace-wide work-item list → an error naming `project_id`. |
| `pql_guard.py` | `disable_pql(mcp)` — the self-hosted `/api/v1` list endpoints ignore `pql`, so `workitem`, `cycle` and `module` reject it with a `ToolError` instead of returning unfiltered results, and PQL is stripped from their descriptions; the `workitem list` line names `project_id` as required, since there is no workspace-wide list. |
| `relations.py` | `restore_relation_types(mcp)` — upstream narrowed `workitem_relation create` to six dependency types; the self-hosted `relations/` route (`IssueRelationCreateSerializer.RELATION_TYPE_CHOICES`) also takes `relates_to` and `duplicate`, so those are routed straight to it, and the `relation_type` enum and footer advertise all eight. |
| `middleware.py` | `ValidateActionArguments` — upstream's stray-argument check with the injected `workspace_slug` set aside. |
| `instructions.py` | `MONETA_INSTRUCTIONS` — replaces upstream's epic recipe (built on `workitem_type` and PQL, both unavailable here). |
| `surface.py` | `apply_moneta_surface(mcp)` — runs `register_moneta_tools`, `disable_pql`, `restore_relation_types`, `add_workspace_arg`, `apply_tool_visibility` (last) and sets the instructions. |
| `workspace.py` | `resolve_workspace(claim, env)` precedence resolver + `_fetch_workspaces()` (raw GET `/api/users/me/workspaces/`). |
| `inject.py` | `add_workspace_arg(mcp)` — injects an optional `workspace_slug` arg onto every workspace-scoped tool without editing tool files. |
| `tools.py` | `register_moneta_tools(mcp)` — the one fork tool, `list_workspaces`. |
| `visibility.py` | `apply_tool_visibility(mcp)` — exposes only the curated resources and actions in `ENABLED_ACTIONS` (see "Tool visibility"). |
| `storage.py` | `build_oauth_storage()` — Valkey **DB 12** + Fernet for the Cognito provider's OAuth state (via `MCP_OAUTH_STORAGE_URL`). |
| `http.py` | `enabled()` / `run()` — the Cognito HTTP server entry point + self-contained JSON logging. |

## Cognito / mPass identity relay

In devstack http mode the `/mcp` endpoint's sole auth layer is FastMCP's Cognito provider (it is
**not** behind mPass; the `/mcp` Traefik router has `strip-auth-headers` only). The provider is a
full OAuth 2.0 authorization server (publishes discovery + a DCR shim over the pre-registered
Cognito app client), so MCP clients (Claude, MCP Inspector) auto-OAuth.

The identity that actually reaches Plane is the Cognito **id_token**, not the access token,
because Cognito access tokens carry **no `email`** and an opaque UUID `username` for federated
users — forwarding one provisions the *wrong* Plane account. `PlaneCognitoProvider`:

1. `_extract_upstream_claims` decodes the id_token at token-exchange time and stashes it.
2. `load_access_token` **re-attaches** it on every request, read from the stored upstream token
   set. fastmcp 3.4 also copies the JWT's `upstream_claims`, but that is an issuance-time
   snapshot that a transparent upstream refresh does not update. Fail-closed: if the id_token
   can't be re-resolved it returns `None` (401) rather than forward the access token.

`bearer_for` then forwards that id_token as the Plane Bearer, which Plane's Traefik + mPass chain
(oauth2-proxy validating against the Cognito JWKS) maps to the same user as the web login.

## The `/api/v1` 401 fix (dual-header)

**Symptom:** `list_workspaces` works; every plane-sdk tool (`list_projects`, …) returns
`HTTP 401: Unauthorized`.

**Cause:** two auth gates stack on the Plane routes, and the SDK only clears one:

| Path | mPass gate (oauth2-proxy) | plane-api gate | Used by |
|---|---|---|---|
| `/api/users/...` (app) | Bearer id_token | SSO header (`X-Auth-Request-Email`) | `list_workspaces`, token minting |
| `/api/v1/...` (external) | Bearer id_token | DRF `APIKeyAuthentication` — wants **`X-Api-Key`**, ignores Bearer | all plane-sdk tools |

The id_token Bearer clears mPass (so the request reaches DRF), but Plane's external API
(`plane/api/middleware/api_authentication.py`) authenticates **only** via the `X-Api-Key` header
against an `APIToken` row. No key → 401 at DRF.

**Fix (`moneta/apitoken.py`, no backend change):**

1. **Mint** a personal Plane `APIToken` for the SSO user once, via the app route
   `POST /api/users/api-tokens/` (id_token relayed through mPass — the same path `list_workspaces`
   uses). The raw token is returned only at create time (`APITokenReadSerializer` excludes it), so
   it's **cached** — Fernet-encrypted in Valkey when `MCP_OAUTH_STORAGE_URL` is set, else
   in-process. Prior `moneta-mcp`-labelled tokens are revoked first so they don't accumulate.
2. **Dual-header client.** plane-sdk forbids passing both `api_key` and `access_token` at
   construction, but all its resources share one mutable `Configuration`. So
   `build_plane_client` constructs with `access_token=<id_token>` then sets
   `client.config.api_key=<minted>`. Every call now sends **both** `Authorization: Bearer
   <id_token>` (satisfies mPass) and `X-Api-Key <minted>` (satisfies DRF). One URL, no
   internal-network route needed.

Non-Cognito paths are untouched: PAT / stdio env → `X-Api-Key`-only; Plane-OAuth → Bearer-only.

## Workspace selection (per-tool arg)

Claude can't send `X-Workspace-Slug` and Cognito tokens have no `workspace_slug` claim. So every
workspace-scoped tool gets an **optional `workspace_slug` argument**, injected at startup by
`moneta/inject.py:add_workspace_arg` via FastMCP's `Tool.from_tool(transform_fn=…)` + `forward()`
(the arg is stashed in a `ContextVar`). This avoids editing every resource tool's signature and keeps
**concurrent Claude conversations isolated** (per-call, not sticky/per-user).

`get_plane_client_context` resolves the workspace with this precedence:

```
per-call workspace_slug arg (ContextVar)  >  token claim (PAT/Plane-OAuth)  >  PLANE_WORKSPACE_SLUG env  >  WorkspaceError
```

The env default applies only to stdio and Cognito connections (no slug claim). A Plane-OAuth or
PAT token is bound to its own claim, even an empty one, as upstream's `tests/test_client.py`
requires; only the per-call arg can pick another workspace.

`list_workspaces` (the discovery tool) is the only tool skipped by the injector. Usage in Claude:
call `list_workspaces` to get slugs, then pass `workspace_slug` per tool call.

## Tool visibility (curated resources and actions)

Upstream advertises one action-dispatch tool per Plane resource (30 tools). `tools/list` here
returns a **fixed curated subset**, and each listed resource advertises only its curated actions.
There is no runtime discovery layer: an LLM sees the real surface on connect and calls it directly.

`moneta/visibility.py` holds `ENABLED_ACTIONS` (resource → actions) plus
`ALWAYS_ENABLED = {list_workspaces}`:

| Resource | Actions |
|---|---|
| `project` | list, retrieve, update |
| `workitem` | list, retrieve, retrieve_by_identifier, search, create, update, manage_assignee, manage_label |
| `workitem_comment` | list, retrieve, create, update, delete |
| `workitem_link` | list, retrieve |
| `workitem_relation` | list, create, delete |
| `cycle` | list, retrieve, create, update, list_workitems, manage_workitems, transfer_workitems, complete |
| `module` | list, retrieve, list_workitems, manage_workitems |
| `label`, `state` | list, retrieve |
| `member` | me, list_workspace, list_project |

- A hidden resource stays **registered but hidden** (`_local_provider.enable(names=…, only=True)`),
  absent from `tools/list` and rejected on `tools/call`.
- A listed resource with hidden actions is wrapped by `restrict_actions`: its `action` enum and
  description name only the curated actions, and any other action raises a `ToolError`. Upstream's
  retired tool names (`LegacyNames`) resolve to the resource with the action pre-bound, so
  `delete_work_item` is refused by the same guard and `list_milestones` is an unknown tool.
- `apply_tool_visibility(mcp)` must run **last** in `apply_moneta_surface`: `disable_pql`,
  `restore_relation_types` and `add_workspace_arg` remove and re-add tools, which would discard an earlier filter.
- `PLANE_MCP_ENABLED_TOOLS` (comma-separated) **replaces** the curated set at runtime. Each entry
  is a resource name (all its actions) or a retired tool name (the one action it maps to);
  `list_workspaces` is always kept.
- `tests/test_visibility.py` asserts the listing equals the curated set exactly, per action.

## Moneta environment variables

| Variable | Purpose |
|---|---|
| `COGNITO_USER_POOL_ID` | Cognito user pool; **its presence switches http mode into Cognito mode** (`moneta.http.enabled()`). |
| `AWS_REGION` | Cognito region (e.g. `ap-southeast-1`). |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Cognito app client (confidential). Secret also derives the Fernet key for OAuth-state + api-token caches. |
| `MCP_OAUTH_STORAGE_URL` | `redis://valkey:6379/12` — Valkey DB 12, Fernet-encrypted OAuth state + minted-token cache. |
| `MCP_JWT_SIGNING_KEY` | FastMCP token signing; fallback Fernet key material for public/PKCE clients. |
| `PLANE_BASE_URL` | Public mPass URL (e.g. `https://pm.foss.local.dev`); used for both app routes and `/api/v1`. |
| `REQUESTS_CA_BUNDLE` | Devstack mkcert CA, so `requests`/plane-sdk trust the self-signed cert. |
| `MCP_LOG_LEVEL` | `DEBUG` raises the fastmcp/moneta loggers (debug traces gate on `moneta.http._configure_logging`). |
| `PLANE_MCP_ENABLED_TOOLS` | Optional comma-separated resource or retired tool names; **replaces** `ENABLED_ACTIONS` (see "Tool visibility"). Unset in devstack. |

## Devstack

- Service in `foss-server-bundle/docker-compose.dev.yml` is **`plane-mcp`**, Compose profile **`mcp`**, router host `pm-mcp.${PLATFORM_DOMAIN}`. The old
  `plane-mcp-server:` block is deprecated.
- No source bind-mount — code is baked into the image, so **rebuild** to pick up changes:
  `make dev.build.plane-mcp && make dev.restart.plane-mcp`.
- Logs: `docker logs plane-mcp`. Tests run without a live backend (httpx/requests mocked):
  `pytest`. `test_integration.py` is skipped unless `PLANE_TEST_API_KEY` and
  `PLANE_TEST_WORKSPACE_SLUG` point at a live server; `test_full_integration` additionally needs
  its write-path actions opted in via `PLANE_MCP_ENABLED_TOOLS`.
