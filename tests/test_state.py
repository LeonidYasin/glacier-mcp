"""Unit tests for glacier_mcp.state.PolygonState."""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from glacier_mcp import geometry as g
from glacier_mcp.state import PolygonState, StateError


def _square() -> Polygon:
    return Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])


def test_initial_state() -> None:
    s = PolygonState(_square())
    assert s.polygon.equals(_square())
    assert s.can_undo() is False
    assert s.can_redo() is False


def test_move_vertex_pushes_undo() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 1.0, 2.0)
    assert s.can_undo() is True
    assert s.get_vertices()[0] == (1.0, 2.0)


def test_undo_restores_previous() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 5.0, 5.0)
    s.undo()
    assert s.get_vertices()[0] == (0.0, 0.0)
    assert s.can_redo() is True


def test_redo_reapplies() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 5.0, 5.0)
    s.undo()
    s.redo()
    assert s.get_vertices()[0] == (5.0, 5.0)


def test_new_edit_clears_redo() -> None:
    s = PolygonState(_square())
    s.move_vertex(0, 1.0, 1.0)
    s.undo()
    s.translate(2.0, 2.0)
    assert s.can_redo() is False


def test_undo_on_empty_raises() -> None:
    s = PolygonState(_square())
    with pytest.raises(StateError):
        s.undo()


def test_apply_short_circuits_on_no_change() -> None:
    s = PolygonState(_square())
    same = s.apply(lambda p: p)
    assert s.can_undo() is False
    assert same is s.polygon


def test_apply_does_not_push_on_raise() -> None:
    s = PolygonState(_square())

    def boom(_p: Polygon) -> Polygon:
        raise g.GeometryError("nope")

    with pytest.raises(g.GeometryError):
        s.apply(boom)
    assert s.can_undo() is False


def test_history_is_bounded() -> None:
    s = PolygonState(_square(), history_size=3)
    for i in range(10):
        s.move_vertex(0, 1.0, 0.0)
    # 3 undo steps max, no more
    count = 0
    while s.can_undo():
        s.undo()
        count += 1
    assert count == 3
