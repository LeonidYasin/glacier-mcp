"""Unit tests for glacier_mcp.state.PolygonState (multi-polygon)."""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from glacier_mcp import geometry as g
from glacier_mcp.state import PolygonState, StateError


def _square(offset: float = 0.0) -> Polygon:
    return Polygon(
        [
            (0.0 + offset, 0.0 + offset),
            (10.0 + offset, 0.0 + offset),
            (10.0 + offset, 10.0 + offset),
            (0.0 + offset, 10.0 + offset),
        ]
    )


def test_initial_state() -> None:
    s = PolygonState(_square())
    assert s.polygon.equals(_square())
    assert len(s) == 1
    assert s.names == ["glacier_1"]
    assert s.can_undo() is False
    assert s.can_redo() is False


def test_move_vertex_pushes_undo() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 0, 1.0, 2.0)
    assert s.can_undo() is True
    assert s.get_vertices(0)[0] == (1.0, 2.0)


def test_undo_restores_previous() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 0, 5.0, 5.0)
    s.undo()
    assert s.get_vertices(0)[0] == (0.0, 0.0)
    assert s.can_redo() is True


def test_redo_reapplies() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 0, 5.0, 5.0)
    s.undo()
    s.redo()
    assert s.get_vertices(0)[0] == (5.0, 5.0)


def test_new_edit_clears_redo() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 0, 1.0, 1.0)
    s.undo()
    s.translate(0, 2.0, 2.0)
    assert s.can_redo() is False


def test_undo_on_empty_raises() -> None:
    s = PolygonState(_square())
    with pytest.raises(StateError):
        s.undo()


def test_apply_does_not_push_on_raise() -> None:
    s = PolygonState(_square())

    def boom(_polys, _names) -> None:
        raise g.GeometryError("nope")

    with pytest.raises(g.GeometryError):
        s.apply(boom)
    assert s.can_undo() is False


def test_history_is_bounded() -> None:
    s = PolygonState(_square(), history_size=3)
    for _i in range(10):
        s.move_vertex(0, 0, 1.0, 0.0)
    count = 0
    while s.can_undo():
        s.undo()
        count += 1
    assert count == 3


def test_add_polygon_returns_index_and_name() -> None:
    s = PolygonState(_square())
    idx = s.add_polygon(_square(offset=100.0))
    assert idx == 1
    assert len(s) == 2
    assert s.names == ["glacier_1", "glacier_2"]


def test_add_polygon_with_custom_name() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0), name="Fedchenko")
    assert s.names[1] == "Fedchenko"


def test_add_polygon_is_one_undo_step() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0))
    assert s.can_undo() is True
    s.undo()
    assert len(s) == 1


def test_remove_polygon_deletes_one() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0))
    s.remove_polygon(0)
    assert len(s) == 1
    assert s.names == ["glacier_2"]


def test_remove_last_polygon_refuses() -> None:
    s = PolygonState(_square())
    with pytest.raises(StateError):
        s.remove_polygon(0)


def test_remove_polygon_bad_index_raises() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0))
    with pytest.raises(StateError):
        s.remove_polygon(5)


def test_move_vertex_targets_correct_polygon() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0))
    s.move_vertex(1, 0, 1.0, 1.0)
    # First polygon untouched
    assert s.get_vertices(0)[0] == (0.0, 0.0)
    # Second polygon moved
    assert s.get_vertices(1)[0] == (101.0, 101.0)


def test_to_geojson_is_feature_collection() -> None:
    s = PolygonState(_square())
    s.add_polygon(_square(offset=100.0))
    gj = s.to_geojson()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 2
    assert gj["features"][0]["properties"]["index"] == 0
    assert gj["features"][1]["properties"]["name"] == "glacier_2"
