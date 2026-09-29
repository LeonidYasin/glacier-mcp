"""Server-side layer registry: the single source of truth for map layers.

Mirrors the design of :mod:`glacier_mcp.state` (``PolygonState``): a small
in-memory collection with a bounded undo/redo history, mutated only through
:meth:`LayerStore.apply`. MCP tools and the REST/WS endpoints all go through
this class, then the hub broadcasts the resulting ``layers_state`` to every
open browser tab.

The browser is a *thin renderer* here: it never owns layer metadata. It only
registers the OpenLayers layer it created for a given id (see the
``layer_register`` WebSocket message) and re-draws whatever the server sends
back.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any


class LayerError(RuntimeError):
    """Raised on invalid layer operations (unknown id, bad index)."""


DEFAULT_HISTORY = 100

# Layer kinds understood by the frontend renderer.
KIND_GEE = "gee"          # XYZ tile template from Earth Engine
KIND_GEOTIFF = "geotiff"  # COG uploaded by the user / tiled by the server
KIND_XYZ = "xyz"          # any other {z}/{x}/{y} template (soviet maps later)
KIND_RASTER = "raster"    # plain georeferenced image
VALID_KINDS = frozenset({KIND_GEE, KIND_GEOTIFF, KIND_XYZ, KIND_RASTER})


def _clamp_opacity(value: float) -> float:
    """Opacity is always stored in the 0..1 range the OL layer expects."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LayerError(f"Opacity must be a number, got {value!r}.") from exc
    return max(0.0, min(1.0, number))


class LayerStore:
    """Holds the ordered list of map layers plus an undo/redo history.

    Ordering is bottom-to-top: ``index 0`` is drawn first (deepest) and the
    last entry is on top. The frontend maps this to OpenLayers z-index via
    ``LAYER_Z_FLOOR`` so manual reordering in the panel and agent-driven
    reordering agree on what "on top" means.
    """

    def __init__(self, history_size: int = DEFAULT_HISTORY) -> None:
        # Start empty: the base map is not a layer, and a fresh session has
        # nothing pushed yet. The panel renders its empty state from this.
        self._layers: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._undo: deque[list[dict[str, Any]]] = deque(maxlen=history_size)
        self._redo: deque[list[dict[str, Any]]] = deque(maxlen=history_size)

    # ---- read -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._layers)

    def __contains__(self, layer_id: object) -> bool:
        return layer_id in self._by_id

    @property
    def layers(self) -> list[dict[str, Any]]:
        """Return a deep-enough copy so callers cannot mutate our state."""
        return [dict(entry) for entry in self._layers]

    def get(self, layer_id: str) -> dict[str, Any]:
        entry = self._by_id.get(layer_id)
        if entry is None:
            raise LayerError(f"Unknown layer id {layer_id!r}.")
        return dict(entry)

    def index_of(self, layer_id: str) -> int:
        for i, entry in enumerate(self._layers):
            if entry["id"] == layer_id:
                return i
        raise LayerError(f"Unknown layer id {layer_id!r}.")

    def list(self) -> list[dict[str, Any]]:
        """JSON-serialisable snapshot used by REST and the WebSocket push."""
        return self.layers

    def to_json(self) -> dict[str, Any]:
        return {"type": "layers_state", "layers": self.layers}

    # ---- write ------------------------------------------------------------

    def _snapshot(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._layers]

    def _rebuild_index(self) -> None:
        self._by_id = {entry["id"]: entry for entry in self._layers}

    def apply(self, fn: Callable[[list[dict[str, Any]]], None]) -> None:
        """Apply a mutation and push the previous ordering onto undo.

        ``fn`` receives a mutable copy of the layer list and mutates it in
        place. If it raises, our state is untouched; if it produces an
        identical list, nothing is recorded on the undo stack.
        """
        new_layers = self._snapshot()
        fn(new_layers)
        if new_layers == self._layers:
            return
        self._undo.append(self._snapshot())
        self._redo.clear()
        self._layers = new_layers
        self._rebuild_index()

    def add(
        self,
        layer_id: str,
        kind: str,
        name: str,
        url: str | None = None,
        opacity: float = 1.0,
        visible: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        """Append a layer. Returns the stored entry.

        Re-adding an existing id replaces that layer in place (this is what
        ``gee_get_basemap`` needs: the same basemap refreshed with a new
        tile URL should not stack up duplicates).
        """
        if not layer_id:
            raise LayerError("Layer id must be a non-empty string.")
        if kind not in VALID_KINDS:
            raise LayerError(
                f"Unknown layer kind {kind!r} (expected one of {sorted(VALID_KINDS)})."
            )

        entry: dict[str, Any] = {
            "id": layer_id,
            "kind": kind,
            "name": name or layer_id,
            "url": url,
            "opacity": _clamp_opacity(opacity),
            "visible": bool(visible),
        }
        entry.update(extra)

        if layer_id in self._by_id:
            self.apply(lambda layers: layers.__setitem__(self.index_of(layer_id), entry))
            return dict(entry)

        self.apply(lambda layers: layers.append(entry))
        return dict(entry)

    def remove(self, layer_id: str) -> None:
        index = self.index_of(layer_id)
        self.apply(lambda layers: layers.pop(index))

    def clear(self) -> None:
        self.apply(lambda layers: layers.clear())

    def set_opacity(self, layer_id: str, opacity: float) -> dict[str, Any]:
        clamped = _clamp_opacity(opacity)

        def _mutate(layers: list[dict[str, Any]]) -> None:
            for entry in layers:
                if entry["id"] == layer_id:
                    entry["opacity"] = clamped
                    return
            raise LayerError(f"Unknown layer id {layer_id!r}.")

        self.apply(_mutate)
        return self.get(layer_id)

    def set_visible(self, layer_id: str, visible: bool) -> dict[str, Any]:
        flag = bool(visible)

        def _mutate(layers: list[dict[str, Any]]) -> None:
            for entry in layers:
                if entry["id"] == layer_id:
                    entry["visible"] = flag
                    return
            raise LayerError(f"Unknown layer id {layer_id!r}.")

        self.apply(_mutate)
        return self.get(layer_id)

    def reorder(self, layer_id: str, to_index: int) -> dict[str, Any]:
        """Move a layer to ``to_index`` (0 = bottom, len-1 = top)."""
        from_index = self.index_of(layer_id)
        count = len(self._layers)
        target = max(0, min(int(to_index), count - 1))
        if target == from_index:
            return self.get(layer_id)

        def _mutate(layers: list[dict[str, Any]]) -> None:
            entry = layers.pop(from_index)
            layers.insert(target, entry)

        self.apply(_mutate)
        return self.get(layer_id)

    # ---- history ----------------------------------------------------------

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> list[dict[str, Any]]:
        if not self._undo:
            raise LayerError("Nothing to undo.")
        self._redo.append(self._snapshot())
        self._layers = self._undo.pop()
        self._rebuild_index()
        return self.layers

    def redo(self) -> list[dict[str, Any]]:
        if not self._redo:
            raise LayerError("Nothing to redo.")
        self._undo.append(self._snapshot())
        self._layers = self._redo.pop()
        self._rebuild_index()
        return self.layers


# Module-level store shared by app.py (REST/WS) and server.py (MCP tools).
layer_store = LayerStore()
