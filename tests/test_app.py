"""Smoke tests for the FastAPI app.

Uses TestClient; does NOT start uvicorn or the MCP server. Verifies the
HTTP surface for the multi-polygon API.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from glacier_mcp import app as app_module
from glacier_mcp.server import get_state

client = TestClient(app_module.app)


def _reset_square() -> None:
    state = get_state()
    state._polygons = [  # noqa: SLF001 - test-only reset
        Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    ]
    state._names = ["glacier_1"]  # noqa: SLF001
    state._undo.clear()  # noqa: SLF001
    state._redo.clear()  # noqa: SLF001


def setup_function(_func) -> None:
    _reset_square()


def test_get_polygons_returns_feature_collection() -> None:
    r = client.get("/api/polygons")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["name"] == "glacier_1"


def test_post_appends_new_polygon() -> None:
    new_geom = {
        "type": "Polygon",
        "coordinates": [[[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0], [100.0, 100.0]]],
    }
    r = client.post("/api/polygons", json={"geometry": new_geom, "name": "second"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["features"]) == 2
    assert body["features"][1]["properties"]["name"] == "second"


def test_post_with_index_replaces_polygon() -> None:
    new_geom = {
        "type": "Polygon",
        "coordinates": [[[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]],
    }
    r = client.post("/api/polygons", json={"geometry": new_geom, "index": 0})
    assert r.status_code == 200
    verts = get_state().get_vertices(0)
    assert (1.0, 1.0) in verts


def test_delete_removes_polygon() -> None:
    # Add a second first so delete is allowed
    ring = [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0], [100.0, 100.0]]
    client.post("/api/polygons", json={"geometry": {"type": "Polygon", "coordinates": [ring]}})
    r = client.delete("/api/polygons/0")
    assert r.status_code == 200
    body = r.json()
    assert len(body["features"]) == 1


def test_export_refuses_without_geotiff() -> None:
    # No upload has happened in this test module, so _current_crs is None.
    r = client.post("/api/export/shapefile", json={})
    assert r.status_code == 400
    assert "Load a GeoTIFF" in r.json()["detail"]
