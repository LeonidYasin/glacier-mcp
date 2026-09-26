"""FastAPI app: serves the OpenLayers UI and the WebSocket bridge.

The UI and the MCP server share a single :class:`PolygonState` via
``server.get_state()``. Any mutation - whether from the mouse or from an
agent calling an MCP tool - triggers a broadcast over ``/ws`` so every
connected browser tab stays in sync.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .server import get_state

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="glacier-mcp")

# ---- WebSocket hub --------------------------------------------------------


class _Hub:
    """Tracks connected WebSocket clients and broadcasts state updates."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


hub = _Hub()


# ---- HTTP endpoints -------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/polygon")
def get_polygon() -> JSONResponse:
    """Current polygon as GeoJSON. Used by the frontend on first load."""
    return JSONResponse(get_state().to_geojson())


@app.post("/api/polygon")
async def set_polygon(payload: dict) -> JSONResponse:
    """Replace the polygon (e.g. after a mouse draw in the browser)."""
    from shapely.geometry import shape as shapely_shape

    geom = shapely_shape(payload["geometry"])
    state = get_state()
    state.replace(geom)
    await hub.broadcast({"type": "polygon", "polygon": state.to_geojson()})
    return JSONResponse(state.to_geojson())


@app.post("/api/vertex/move")
async def move_vertex(payload: dict) -> JSONResponse:
    """Move vertex ``index`` by (dx, dy). Used by drag handlers."""
    state = get_state()
    state.move_vertex(int(payload["index"]), float(payload["dx"]), float(payload["dy"]))
    await hub.broadcast({"type": "polygon", "polygon": state.to_geojson()})
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    # Send current state immediately so a fresh client is in sync.
    await ws.send_text(
        json.dumps({"type": "polygon", "polygon": get_state().to_geojson()})
    )
    try:
        while True:
            # We do not expect messages from the client yet; this keeps the
            # connection alive and lets us react to disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)


# ---- static frontend ------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the map UI server on ``host:port``."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
