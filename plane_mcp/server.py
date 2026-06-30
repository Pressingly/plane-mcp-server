"""FastMCP server factories for the three supported transports."""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from mcp.types import Icon

from plane_mcp.auth import PlaneHeaderAuthProvider, PlaneOAuthProvider
from plane_mcp.storage import build_token_store
from plane_mcp.tools import register_tools

_INSTRUCTIONS = (
    "Manages projects, work items, cycles, modules, and more in Plane, a "
    "project management tool.\n\n"
    "Tool discovery: only a small default set of tools is enabled on startup. "
    "If the current tools cannot fulfill a request, call list_available_tools "
    "to see the full catalog of available tools (cycles, modules, pages, "
    "labels, states, initiatives, etc.), then call enable_tools to activate "
    "the ones you need before using them.\n\n"
    "Getting started: use list_workspaces to discover workspace slugs, then "
    "list_projects to find projects. Pass workspace_slug on each tool call "
    "when working in multi-workspace mode."
)


def get_oauth_mcp(base_path: str = "/") -> FastMCP:
    """Build the FastMCP instance for the OAuth HTTP / SSE transports."""
    oauth_mcp = FastMCP(
        "Plane MCP Server",
        instructions=_INSTRUCTIONS,
        icons=[Icon(src="https://plane.so/favicon.ico", alt="Plane MCP Server")],
        website_url="https://plane.so",
        auth=PlaneOAuthProvider(
            client_id=os.getenv("PLANE_OAUTH_PROVIDER_CLIENT_ID", ""),
            client_secret=os.getenv("PLANE_OAUTH_PROVIDER_CLIENT_SECRET", ""),
            base_url=f"{os.getenv('PLANE_OAUTH_PROVIDER_BASE_URL')}{base_path}",
            plane_base_url=os.getenv("PLANE_BASE_URL", ""),
            plane_internal_base_url=os.getenv("PLANE_INTERNAL_BASE_URL", ""),
            client_storage=build_token_store(),
            required_scopes=["read", "write"],
            allowed_client_redirect_uris=[
                # Localhost only for http (dynamic ports from MCP clients)
                "http://localhost:*",
                "http://localhost:*/*",
                "http://127.0.0.1:*",
                "http://127.0.0.1:*/*",
                # Known MCP client custom protocol schemes
                "cursor://*",
                "vscode://*",
                "vscode-insiders://*",
                "windsurf://*",
                "claude://*",
            ],
        ),
    )
    oauth_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(oauth_mcp)
    return oauth_mcp


def get_header_mcp():
    header_mcp = FastMCP(
        "Plane MCP Server (header-http)",
        instructions=_INSTRUCTIONS,
        auth=PlaneHeaderAuthProvider(
            required_scopes=["read", "write"],
        ),
    )
    header_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(header_mcp)
    return header_mcp


def get_stdio_mcp():
    stdio_mcp = FastMCP(
        "Plane MCP Server (stdio)",
        instructions=_INSTRUCTIONS,
    )
    stdio_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(stdio_mcp)
    return stdio_mcp
