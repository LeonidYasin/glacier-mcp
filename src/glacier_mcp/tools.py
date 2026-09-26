"""Public API for the MCP tools.

The tool functions themselves live in :mod:`glacier_mcp.server`, where
they are registered on the FastMCP instance. This module re-exports them
so they can be imported without touching the server module (and so tests
that do not want to spin up the HTTP transport can call them directly).
"""

from __future__ import annotations

from .server import (
    add_vertex,
    delete_vertex,
    get_polygon,
    get_state,
    get_vertices,
    move_vertex,
    redo,
    smooth_polygon,
    translate_polygon,
    undo,
)

__all__ = [
    "add_vertex",
    "delete_vertex",
    "get_polygon",
    "get_state",
    "get_vertices",
    "move_vertex",
    "redo",
    "smooth_polygon",
    "translate_polygon",
    "undo",
]
