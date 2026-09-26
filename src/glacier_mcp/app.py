"""FastAPI app: serves the OpenLayers UI and the WebSocket bridge.

Runs on port 8765 by default and shares the same :class:`PolygonState`
instance as the MCP server. Edits made through the map are pushed to
the agent on the next MCP call, and edits made by the agent are pushed
back to every connected browser over ``/ws``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .server import get_state

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="glacier-mcp", version="0.0.1")

# ---- WebSocket fan-out ----------------------------------------------------


class Hub:
    """Tracks connected browser sockets and broadcasts state snapshots."""

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

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload)
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in self._clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


hub = Hub()


def _snapshot() -> dict[str, Any]:
    """Current polygon + version counter the client can use for reconciliation."""
    state = get_state()
    return {
        "type": "snapshot",
        "polygon": state.to_geojson(),
        "vertices": [
            {"index": i, "x": x, "y": y}
            for i, (x, y) in enumerate(state.get_vertices())
        ],
        "can_undo": state.can_undo(),
        "can_redo": state.can_redo(),
    }


async def notify_change() -> None:
    """Broadcast the current state to all browsers.

    Called by the MCP tool wrappers (when they mutate state) and by the
    HTTP endpoints below. Kept as a plain coroutine so it works from both
    sync and async contexts by scheduling on the running loop.
    """
    await hub.broadcast(_snapshot())


# ---- HTTP -----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/snapshot")
def api_snapshot() -> dict[str, Any]:
    return _snapshot()


# Serve the rest of the frontend (main.js, style.css) as static files.
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---- WebSocket ------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        # Send the current state immediately so the map can render on load.
        await ws.send_text(json.dumps(_snapshot()))
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            await _handle_client_message(msg)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)


async def _handle_client_message(msg: dict[str, Any]) -> None:
    """Handle one message from the browser and re-broadcast if state changed.

    The client sends the same shape as the MCP tools, so the map and the
    agent go through the exact same code path in ``PolygonState``.
    """
    action = msg.get("action")
    state = get_state()
    try:
        if action == "replace":
            # Full polygon replacement (mouse draw).
            from shapely.geometry import Polygon as ShapelyPolygon

            ring = msg["ring"]
            state.replace(ShapelyPolygon(ring))
        elif action == "move_vertex":
            state.move_vertex(msg["index"], msg["dx"], msg["dy"])
        elif action == "add_vertex":
            state.add_vertex(msg["index"], (msg["x"], msg["y"]))
        elif action == "delete_vertex":
            state.delete_vertex(msg["index"])
        elif action == "translate":
            state.translate(msg["dx"], msg["dy"])
        elif action == "smooth":
            state.smooth(msg.get("tolerance", 0.0))
        elif action == "undo":
            state.undo()
        elif action == "redo":
            state.redo()
        else:
            return  # unknown action: ignore
    except Exception as exc:
        # Do not kill the socket on a bad edit; report it to the client.
        await hub.broadcast({"type": "error", "message": str(exc)})
        return

    await notify_change()
