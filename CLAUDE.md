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

Three factory functions (`get_oauth_mcp`, `get_header_mcp`, `get_stdio_mcp`) each create a `FastMCP` instance, register all tools, and configure the appropriate auth provider. OAuth/HTTP modes use Redis for token storage (falls back to in-memory).

### Client Context (`client.py`)

`get_plane_client_context()` returns a `PlaneClientContext(client, workspace_slug)` namedtuple. It resolves credentials from the MCP request context (OAuth token or header API key) or from environment variables (stdio mode). Prefers `PLANE_INTERNAL_BASE_URL` for server-to-server calls.

### Authentication (`auth/`)

- `PlaneOAuthProvider` — Full OAuth flow with token verification against the Plane API.
- `PlaneHeaderAuthProvider` — Simple header-based auth using `x-api-key` and `x-workspace-slug` headers.

### Tools (`tools/`)

19 tool modules organized by Plane domain (projects, work_items, cycles, modules, etc.), totaling 55+ tools. Each module exports a `register_*_tools(mcp: FastMCP)` function called from `tools/__init__.py`.

**Tool pattern:**
```python
def register_*_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def tool_name(param: str, optional_param: str | None = None) -> SomePlaneModel:
        """Docstring with Args and Returns sections."""
        client, workspace_slug = get_plane_client_context()
        return client.endpoint.operation(workspace_slug=workspace_slug, ...)
```

Tools return Pydantic models from `plane-sdk` and use Python 3.10+ union syntax (`str | None`).

### Testing

Integration tests in `tests/test_integration.py` use `FastMCP.Client` with `StreamableHttpTransport`. Tests run against a live Plane instance — configure via `.env.test` (copy to `.env.test.local` with real values).

## Key Environment Variables

| Variable | Required For | Purpose |
|---|---|---|
| `PLANE_API_KEY` | stdio | API key for authentication |
| `PLANE_WORKSPACE_SLUG` | stdio | Target workspace |
| `PLANE_BASE_URL` | all (default: https://api.plane.so) | Plane API URL |
| `PLANE_INTERNAL_BASE_URL` | http/sse (optional) | Internal URL for server-to-server calls |
| `REDIS_HOST` / `REDIS_PORT` | http/sse (optional) | Token storage (falls back to in-memory) |
| `PLANE_OAUTH_PROVIDER_*` | http/sse OAuth | OAuth client credentials and base URL |

---

# CLAUDE.md MONETA

This half documents the **Moneta fork** — the changes that make this server run inside the
`foss-server-bundle` devstack behind AWS Cognito / mPass SSO, alongside `surfsense-mcp-server`.
Everything above is upstream `makeplane/plane-mcp-server`; everything below is fork-specific.

## Branch & commit policy

- **`main` is the only live branch.** It tracks upstream (e.g. tag `v2.5.0-phoenix-838c572`).
  `foss-main`, `jawad/*`, `wip/*` are **deprecated** — do not reference or forward-port them,
  even though `foss-server-bundle`'s `make dev.clone.plane-mcp` still clones `foss-main` and that
  branch carries a stale Cognito integration.
- The Moneta integration is **uncommitted by policy** ("we will NOT commit"). It lives in the
  working tree on top of `main`.

## Fork-isolation rule (read before editing)

All fork logic lives in the **`plane_mcp/moneta/`** subpackage. Upstream modules carry only
**tiny additive hooks** (one import + one call each), so upstream pulls stay conflict-free. The
~20 files under `plane_mcp/tools/` are **byte-pristine** and must stay that way.

**When adding a fork feature: put the logic in `plane_mcp/moneta/` and add a one-line hook. Do
not edit upstream modules in place.** Accept DRY duplication with upstream over merge pain.

Upstream files touched (the entire upstream footprint):

| File | Hook |
|---|---|
| `__main__.py` | http mode → `moneta.http.run()` when `moneta.http.enabled()` (i.e. `COGNITO_USER_POOL_ID` set), else upstream Plane-OAuth+SSE |
| `client.py` | `get_plane_client_context` calls `bearer_for(...)`, `resolve_workspace(...)`, and `build_plane_client(...)` |
| `tools/__init__.py` | `register_moneta_tools(mcp)` + `add_workspace_arg(mcp)` + `apply_tool_visibility(mcp)` at the end of `register_tools`; `register_milestone_tools` is **not** called (milestones are unsupported by the community edition — the file itself stays byte-pristine) |

## `plane_mcp/moneta/` modules

| Module | Responsibility |
|---|---|
| `cognito.py` | `PlaneCognitoProvider(AWSCognitoProvider)` — captures the Cognito **id_token** and stashes `{id_token, email, cognito:username}` under the issued JWT's `upstream_claims`. |
| `client.py` | `bearer_for(token, claims)` (forwards the id_token), `plane_request_auth()` (base_url + headers for raw **app-API** calls). |
| `apitoken.py` | Mints/caches a Plane `APIToken` and builds the dual-header `PlaneClient` (see "The `/api/v1` 401 fix"). |
| `workspace.py` | `resolve_workspace(claim, env)` precedence resolver + `_fetch_workspaces()` (raw GET `/api/users/me/workspaces/`). |
| `inject.py` | `add_workspace_arg(mcp)` — injects an optional `workspace_slug` arg onto every workspace-scoped tool without editing tool files. |
| `tools.py` | `register_moneta_tools(mcp)` — the one fork tool, `list_workspaces`. |
| `visibility.py` | `apply_tool_visibility(mcp)` — exposes only the curated `ENABLED_TOOLS` set on `tools/list` (see "Tool visibility"). |
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
2. `load_access_token` **re-attaches** it on every request — required because fastmcp 3.2.0's
   `OAuthProxy.load_access_token` is a token-swap that drops the embedded `upstream_claims`.
   Fail-closed: if the id_token can't be re-resolved it returns `None` (401) rather than forward
   the access token.

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
(the arg is stashed in a `ContextVar`). This avoids editing the ~144 tool signatures and keeps
**concurrent Claude conversations isolated** (per-call, not sticky/per-user).

`get_plane_client_context` resolves the workspace with this precedence:

```
per-call workspace_slug arg (ContextVar)  >  token claim (PAT/Plane-OAuth)  >  PLANE_WORKSPACE_SLUG env  >  WorkspaceError
```

`list_workspaces` (the discovery tool) is the only tool skipped by the injector. Usage in Claude:
call `list_workspaces` to get slugs, then pass `workspace_slug` per tool call.

## Tool visibility (standard MCPO listing)

`tools/list` returns a **fixed curated set** — the MCPO convention, where each listed tool becomes
one generated HTTP endpoint. There is no runtime discovery layer: the former `list_available_tools`
/ `enable_tools` / `execute_tool` meta tools were removed, so an LLM sees the real tool list on
connect and calls tools directly.

`moneta/visibility.py` holds `ENABLED_TOOLS` (41 names) plus `ALWAYS_ENABLED = {list_workspaces}`,
and applies them via `mcp._local_provider.enable(names=…, only=True, components={"tool"})`. The
other 66 tools stay **registered but hidden** — absent from `tools/list` and rejected on
`tools/call` — so re-activating one is a one-line change, not a re-implementation.

- `apply_tool_visibility(mcp)` must run **last** in `register_tools`: `add_workspace_arg` does
  `remove_tool()` + `add_tool()` per tool, which would discard an earlier filter.
- `PLANE_MCP_ENABLED_TOOLS` (comma-separated) **replaces** the curated set at runtime;
  `list_workspaces` is unioned in unconditionally so a partial override can't brick workspace
  resolution.
- Milestone tools are not registered at all (community edition lacks the endpoints).
- `tests/test_visibility.py` asserts `tools/list` equals the whitelist exactly and that a hidden
  tool (`create_project`) is rejected on call.

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
| `PLANE_MCP_ENABLED_TOOLS` | Optional comma-separated tool whitelist; **replaces** `ENABLED_TOOLS` (see "Tool visibility"). Unset in devstack. |

## Devstack

- Service in `foss-server-bundle/docker-compose.dev.yml` is **`plane-mcp`** (modeled on
  `surfsense-mcp`), Compose profile **`mcp`**, router host `pm-mcp.${PLATFORM_DOMAIN}`. The old
  `plane-mcp-server:` block is deprecated.
- No source bind-mount — code is baked into the image, so **rebuild** to pick up changes:
  `make dev.build.plane-mcp && make dev.restart.plane-mcp`.
- Logs: `docker logs plane-mcp`. Tests run without a live backend (httpx/requests mocked):
  `pytest` (the 2 `test_integration` tests need live-backend env and otherwise fail by design).
