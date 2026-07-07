"""Moneta fork additions for the Plane MCP Server.

All Pressingly/Moneta-specific code lives in this package so the upstream
``plane_mcp`` modules stay as close to the makeplane original as possible —
upstream pulls then touch only a handful of one-line hooks, not the fork logic.

Hooks into upstream (kept intentionally tiny):

* ``plane_mcp.__main__`` — http mode delegates to :func:`plane_mcp.moneta.http.run`
  when :func:`plane_mcp.moneta.http.enabled` is true (Cognito/devstack).
* ``plane_mcp.client`` — forwards the Cognito id_token via
  :func:`plane_mcp.moneta.client.bearer_for`.
* ``plane_mcp.tools`` — registers extra tools via
  :func:`plane_mcp.moneta.tools.register_moneta_tools`.

Everything else (the Cognito provider, OAuth-state storage, the Cognito HTTP
app, ``list_workspaces``) is defined here and imported by those hooks. Some
small helpers are duplicated from upstream rather than imported, by design —
duplication is cheaper than recurring merge conflicts.
"""
