"""Tests for the server-owned layer registry (:mod:`glacier_mcp.layers`)."""

from __future__ import annotations

import pytest

from glacier_mcp.layers import LayerError, LayerStore


@pytest.fixture()
def store() -> LayerStore:
    return LayerStore()


def test_starts_empty(store: LayerStore) -> None:
    assert len(store) == 0
    assert store.list() == []
    # The empty state is what the panel renders as its placeholder, so the
    # JSON envelope must still be well-formed.
    assert store.to_json() == {"type": "layers_state", "layers": []}


def test_add_appends_in_order(store: LayerStore) -> None:
    store.add("a", "gee", "GEE A")
    store.add("b", "geotiff", "GeoTIFF B")
    ids = [layer["id"] for layer in store.list()]
    assert ids == ["a", "b"]
    assert len(store) == 2


def test_add_defaults(store: LayerStore) -> None:
    entry = store.add("a", "xyz", "Layer A")
    assert entry["url"] is None
    assert entry["opacity"] == 1.0
    assert entry["visible"] is True


def test_add_same_id_replaces_in_place(store: LayerStore) -> None:
    store.add("base", "gee", "Old", url="http://old/{z}/{x}/{y}")
    store.add("other", "xyz", "Other")
    store.add("base", "gee", "New", url="http://new/{z}/{x}/{y}")
    # Refreshing a basemap must not stack a duplicate.
    assert len(store) == 2
    assert [layer["id"] for layer in store.list()] == ["base", "other"]
    assert store.get("base")["name"] == "New"
    assert store.get("base")["url"] == "http://new/{z}/{x}/{y}"


def test_add_rejects_unknown_kind(store: LayerStore) -> None:
    with pytest.raises(LayerError):
        store.add("a", "hologram", "Nope")


def test_add_rejects_empty_id(store: LayerStore) -> None:
    with pytest.raises(LayerError):
        store.add("", "xyz", "Nope")


def test_remove(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    store.add("b", "xyz", "B")
    store.remove("a")
    assert [layer["id"] for layer in store.list()] == ["b"]
    with pytest.raises(LayerError):
        store.remove("a")


def test_remove_last_allowed(store: LayerStore) -> None:
    store.add("only", "xyz", "Only")
    store.remove("only")
    assert len(store) == 0


def test_set_opacity_clamps(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    assert store.set_opacity("a", 0.4)["opacity"] == pytest.approx(0.4)
    assert store.set_opacity("a", 5.0)["opacity"] == 1.0
    assert store.set_opacity("a", -3.0)["opacity"] == 0.0


def test_set_opacity_rejects_non_number(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    with pytest.raises(LayerError):
        store.set_opacity("a", "half")  # type: ignore[arg-type]


def test_set_opacity_unknown_id(store: LayerStore) -> None:
    with pytest.raises(LayerError):
        store.set_opacity("ghost", 0.5)


def test_set_visible(store: LayerStore) -> None:
    store.add("a", "xyz", "A", visible=True)
    assert store.set_visible("a", False)["visible"] is False
    assert store.set_visible("a", True)["visible"] is True
    with pytest.raises(LayerError):
        store.set_visible("ghost", False)


def test_reorder_moves_layer(store: LayerStore) -> None:
    for name in ("a", "b", "c"):
        store.add(name, "xyz", name.upper())
    store.reorder("a", 2)
    assert [layer["id"] for layer in store.list()] == ["b", "c", "a"]


def test_reorder_clamps(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    store.add("b", "xyz", "B")
    # Big index means "bring to front".
    store.reorder("a", 99)
    assert [layer["id"] for layer in store.list()] == ["b", "a"]
    # Negative / zero means "send to back".
    store.reorder("a", -5)
    assert [layer["id"] for layer in store.list()] == ["a", "b"]


def test_reorder_unknown_id(store: LayerStore) -> None:
    with pytest.raises(LayerError):
        store.reorder("ghost", 0)


def test_undo_redo_round_trip(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    store.add("b", "xyz", "B")
    assert store.can_undo()
    store.undo()
    assert [layer["id"] for layer in store.list()] == ["a"]
    assert store.can_redo()
    store.redo()
    assert [layer["id"] for layer in store.list()] == ["a", "b"]


def test_undo_empty_raises(store: LayerStore) -> None:
    with pytest.raises(LayerError):
        store.undo()
    with pytest.raises(LayerError):
        store.redo()


def test_list_returns_copies(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    snapshot = store.list()
    snapshot[0]["name"] = "mutated"
    # Mutating the returned snapshot must not leak into the store.
    assert store.get("a")["name"] == "A"


def test_to_json_envelope(store: LayerStore) -> None:
    store.add("a", "xyz", "A")
    payload = store.to_json()
    assert payload["type"] == "layers_state"
    assert payload["layers"][0]["id"] == "a"


def test_extra_fields_are_preserved(store: LayerStore) -> None:
    entry = store.add("a", "gee", "A", scene_id="S2B_...", preset="ndsi")
    assert entry["scene_id"] == "S2B_..."
    assert store.get("a")["preset"] == "ndsi"
