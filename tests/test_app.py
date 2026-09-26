"""Tests for glacier_mcp.app.

We only exercise the synchronous pieces here (``/api/snapshot``) to keep
the test suite free of a running event loop. The WebSocket path is
covered manually for now; adding ``httpx.AsyncClient`` + ``websockets``
based tests is a follow-up.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from glacier_mcp.app import app


def test_snapshot_endpoint_returns_polygon() -> None:
    client = TestClient(app)
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "snapshot"
    assert body["polygon"]["type"] == "Feature"
    assert body["polygon"]["geometry"]["type"] == "Polygon"
    assert isinstance(body["vertices"], list)
    assert len(body["vertices"]) >= 3
    assert "can_undo" in body and "can_redo" in body
