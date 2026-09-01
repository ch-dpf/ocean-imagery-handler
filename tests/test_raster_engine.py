"""Core raster engine tests."""

from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_array
from app.services.raster.info import raster_info_json
from app.services.raster.resample import sample_bilinear
from tests.raster_fixtures import write_rgb_geotiff_4326


def test_geotiff_roundtrip_window_read(tmp_path: Path):
    path = tmp_path / "round.tif"
    data = np.arange(32 * 32 * 3, dtype=np.uint8).reshape(32, 32, 3)
    write_geotiff_array(
        path,
        data,
        affine=Affine.north_up(0.0, 10.0, 1.0, 1.0),
        crs=CRS.from_epsg(4326),
        block_size=16,
    )
    with GeoTiffReader(path) as src:
        assert src.width == 32
        assert src.height == 32
        assert src.samples == 3
        window = src.read_window(8, 8, 10, 12)
        np.testing.assert_array_equal(window, data[8:18, 8:20])
        outside = src.read_window(-4, -4, 8, 8)
        assert outside.shape == (8, 8, 3)
        assert np.all(outside[:4, :4] == 0)
        np.testing.assert_array_equal(outside[4:, 4:], data[:4, :4])


def test_raster_info_json_wgs84_extent(tmp_path: Path):
    dataset = write_rgb_geotiff_4326(tmp_path / "src.tif", width=20, height=10, west=1.0, north=2.0, pixel_deg=0.1)
    info = raster_info_json(dataset)
    assert info["size"] == [20, 10]
    assert info["coordinateSystem"]["epsg"] == 4326
    ring = info["wgs84Extent"]["coordinates"][0]
    assert ring
    bounds = info["wgs84Bounds"]
    assert bounds[0] == 1.0
    assert bounds[3] == 2.0


def test_sample_bilinear_identity():
    src = np.array([[[10], [20]], [[30], [40]]], dtype=np.uint8)
    rows = np.array([[0.0, 0.0], [1.0, 1.0]])
    cols = np.array([[0.0, 1.0], [0.0, 1.0]])
    out = sample_bilinear(src, rows, cols)
    np.testing.assert_allclose(out[:, :, 0], [[10, 20], [30, 40]], atol=1e-5)
