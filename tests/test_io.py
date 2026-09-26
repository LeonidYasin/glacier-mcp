"""Tests for glacier_mcp.io.

A tiny GeoTIFF is synthesised per-test so nothing binary lives in the
repo. The exported shapefile is written to pytest's tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from glacier_mcp import io as gio

UTM_33N = CRS.from_epsg(32633)  # typical Arctic/Alpine UTM zone


def _write_tiny_geotiff(path: Path) -> None:
    """Create a 4x4, 1-band UTM 33N GeoTIFF."""
    data = np.arange(16, dtype=np.uint8).reshape(1, 4, 4)
    transform = from_origin(west=500_000, north=5_000_000, xsize=10, ysize=10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=4,
        width=4,
        count=1,
        dtype=data.dtype,
        crs=UTM_33N.to_wkt(),
        transform=transform,
    ) as dst:
        dst.write(data)


def test_load_geotiff_returns_raster(tmp_path: Path) -> None:
    tif = tmp_path / "tiny.tif"
    _write_tiny_geotiff(tif)
    r = gio.load_geotiff(tif)
    assert r.data.shape == (1, 4, 4)
    assert r.shape == (4, 4)
    assert r.crs.to_epsg() == 32633
    assert r.path == tif.resolve()


def test_load_geotiff_missing_file() -> None:
    with pytest.raises(gio.IOError):
        gio.load_geotiff("/definitely/does/not/exist.tif")


def test_export_shapefile_writes_all_sidecars(tmp_path: Path) -> None:
    poly = Polygon([(500_000, 5_000_000), (500_100, 5_000_000), (500_100, 4_999_900)])
    written = gio.export_shapefile(poly, UTM_33N, tmp_path, name="glacier")
    names = {p.suffix for p in written}
    # ESRI Shapefile sidecars that make the file usable elsewhere.
    assert {".shp", ".shx", ".dbf", ".prj"} <= names
    prj = tmp_path / "glacier.prj"
    assert prj.exists() and prj.stat().st_size > 0


def test_export_shapefile_refuses_empty_polygon(tmp_path: Path) -> None:
    with pytest.raises(gio.IOError):
        gio.export_shapefile(Polygon(), UTM_33N, tmp_path)


def test_export_shapefile_refuses_epsg_4326(tmp_path: Path) -> None:
    poly = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
    with pytest.raises(gio.IOError):
        gio.export_shapefile(poly, CRS.from_epsg(4326), tmp_path)


def test_polygon_to_crs_roundtrip() -> None:
    """Reprojecting a polygon UTM->4326->UTM must come back to itself."""
    poly = Polygon(
        [
            (500_000, 5_000_000),
            (500_100, 5_000_000),
            (500_100, 4_999_900),
            (500_000, 4_999_900),
        ]
    )
    latlon = gio.polygon_to_crs(poly, UTM_33N, CRS.from_epsg(4326))
    back = gio.polygon_to_crs(latlon, CRS.from_epsg(4326), UTM_33N)
    # 1e-3 m is well below raster resolution at 2 m/pixel.
    assert back.equals_exact(poly, tolerance=1e-3)
