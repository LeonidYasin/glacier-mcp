"""Unit tests for glacier_mcp.geometry."""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from glacier_mcp import geometry as g


def _square() -> Polygon:
    return Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])


def test_vertices_no_closing_duplicate() -> None:
    assert g.vertices(_square()) == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 10.0),
        (0.0, 10.0),
    ]


def test_move_vertex_updates_coordinate() -> None:
    p = g.move_vertex(_square(), 0, 5.0, -3.0)
    assert g.vertices(p)[0] == (5.0, -3.0)


def test_move_vertex_negative_index() -> None:
    p = g.move_vertex(_square(), -1, 1.0, 1.0)
    assert g.vertices(p)[-1] == (1.0, 11.0)


def test_move_vertex_out_of_range() -> None:
    with pytest.raises(g.GeometryError):
        g.move_vertex(_square(), 4, 0.0, 0.0)


def test_add_vertex_before_index() -> None:
    p = g.add_vertex(_square(), 1, (5.0, -1.0))
    assert g.vertices(p)[1] == (5.0, -1.0)


def test_add_vertex_at_end() -> None:
    p = g.add_vertex(_square(), 4, (5.0, 5.0))
    assert len(g.vertices(p)) == 5


def test_delete_vertex() -> None:
    p = g.delete_vertex(_square(), 0)
    assert len(g.vertices(p)) == 3


def test_delete_vertex_refuses_when_only_three_left() -> None:
    triangle = Polygon([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    with pytest.raises(g.GeometryError):
        g.delete_vertex(triangle, 0)


def test_translate_polygon() -> None:
    p = g.translate_polygon(_square(), 100.0, -50.0)
    xs, ys = zip(*g.vertices(p), strict=True)
    assert min(xs) == 100.0
    assert max(xs) == 110.0
    assert min(ys) == -50.0
    assert max(ys) == -40.0


def test_smooth_polygon_increases_vertex_count() -> None:
    smoothed = g.smooth_polygon(_square())
    assert len(g.vertices(smoothed)) == 8
    assert smoothed.is_valid
