"""Smoke tests for the FastAPI app.

Uses TestClient; does NOT start uvicorn or the MCP server. Verifies that
the HTTP surface responds and that POSTing a polygon updates the shared
state.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from glacier_mcp import app as app_module
from glacier_mcp.server import get_state


client = TestClient(app_module.app)


def _reset_square() -> None:
    state = get_state()
    state._polygon = Polygon(  # noqa: SLF001 - test-only reset
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    )
    state._undo.clear()  # noqa: SLF001
    state._redo.clear()  # noqa: SLF001


def setup_function(_func) -> None:  # noqa: ANN001 - pytest hook
    _reset_square()


def test_get_polygon_returns_geojson() -> None:
    r = client.get("/api/polygon")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "Feature"
    assert body["geometry"]["type"] == "Polygon"


def test_set_polygon_replaces_state() -> None:
    new_geom = {
        "type": "Polygon",
        "coordinates": [[[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]],
    }
    r = client.post(
        "/api/polygon",
        json={"type": "Feature", "properties": {}, "geometry": new_geom},
    )
    assert r.status_code == 200
    verts = get_state().get_vertices()
    assert (1.0, 1.0) in verts
