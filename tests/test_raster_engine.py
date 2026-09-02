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


def test_remap_and_same_crs_warp(tmp_path: Path):
    from app.services.raster.resample import remap_image
    from app.services.raster.warp import warp_window

    src_arr = np.arange(16 * 16 * 3, dtype=np.uint8).reshape(16, 16, 3)
    path = tmp_path / "src.tif"
    affine = Affine.north_up(0.0, 16.0, 1.0, 1.0)
    write_geotiff_array(path, src_arr, affine=affine, crs=CRS.from_epsg(4326), block_size=16)
    yy, xx = np.mgrid[0:16, 0:16].astype(np.float32)
    remapped = remap_image(src_arr, xx + 0.5, yy + 0.5, "bilinear")
    np.testing.assert_array_equal(remapped, src_arr)

    with GeoTiffReader(path) as src:
        out = warp_window(
            src,
            affine,
            CRS.from_epsg(4326),
            0,
            0,
            16,
            16,
            "bilinear",
            add_alpha=False,
        )
    assert out.shape[0] == 16 and out.shape[1] == 16
    np.testing.assert_allclose(out[:, :, :3].astype(np.float32), src_arr.astype(np.float32), atol=1)


def test_ordered_parallel_map_preserves_order():
    from app.services.raster.parallel import ordered_parallel_map, run_unordered

    doubled = list(ordered_parallel_map(range(24), lambda value: value * 2, workers=4))
    assert doubled == [value * 2 for value in range(24)]

    seen: list[int] = []
    run_unordered(range(10), seen.append, workers=3)
    assert sorted(seen) == list(range(10))


def test_overviews_used_for_coarse_warp(tmp_path: Path):
    from app.services.raster.overviews import add_overviews
    from app.services.raster.warp import warp_window

    height = width = 64
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[:, :, 0] = np.arange(width, dtype=np.uint8)[None, :]
    data[:, :, 1] = np.arange(height, dtype=np.uint8)[:, None]
    data[:, :, 2] = 80
    path = tmp_path / "src.tif"
    affine = Affine.north_up(0.0, float(height), 1.0, 1.0)
    write_geotiff_array(path, data, affine=affine, crs=CRS.from_epsg(4326), block_size=16)
    assert add_overviews(path, levels=(2, 4), block_size=16, workers=2) is not None

    with GeoTiffReader(path) as src:
        assert src.overview_scales == [2, 4]
        assert src.select_level(8, 8, 8, 8).scale == 1
        assert src.select_level(64, 64, 8, 8).scale == 4
        dst_affine = Affine.north_up(0.0, float(height), 8.0, 8.0)
        out = warp_window(src, dst_affine, CRS.from_epsg(4326), 0, 0, 8, 8, "bilinear")
        assert out.shape[:2] == (8, 8)
        assert out[0, -1, 0] > out[0, 0, 0]
        assert out[-1, 0, 1] > out[0, 0, 1]
        np.testing.assert_allclose(out[:, :, 2].astype(np.float32), 80, atol=2)
        assert src.select_level(28, 28, 8, 8).scale == 2


def test_destination_grid_and_zoom_are_formula_based():
    import math

    from app.services.raster.crsutil import EARTH_HALF, destination_pixel_size, grid_dimension
    from app.services.raster.tiles import auto_max_zoom_mercator

    affine = Affine.north_up(0.0, 10.0, 0.1, 0.1)
    px, py = destination_pixel_size(CRS.from_epsg(4326), CRS.from_epsg(3857), affine, 20, 10)
    assert px > 0 and py > 0
    assert grid_dimension(10.0, 2.0) == 5
    assert grid_dimension(10.0, 3.0) == 4
    ratio = (2 * EARTH_HALF) / (px * 256)
    assert auto_max_zoom_mercator(px, 256) == max(0, math.ceil(math.log2(ratio) - 1e-12))


def test_north_up_requires_exact_zero_shear():
    assert Affine.north_up(0.0, 1.0, 1.0, 1.0).is_north_up()
    tilted = Affine(a=1.0, b=1e-12, c=0.0, d=0.0, e=-1.0, f=1.0)
    assert not tilted.is_north_up()


def test_save_tile_keeps_fully_transparent_png(tmp_path: Path):
    from app.schemas import TileFormat
    from app.services.raster.tiles import _save_tile

    blank = np.zeros((16, 16, 4), dtype=np.uint8)
    path = tmp_path / "0" / "0" / "0.png"
    assert _save_tile(path, blank, TileFormat.PNG) is True
    assert path.is_file() and path.stat().st_size > 0


def test_mosaic_pyramid_writes_transparent_low_zooms(tmp_path: Path):
    from app.schemas import TileProfile, TileScheme, TilingOptions
    from app.services.preprocessor import preprocess_imagery
    from app.schemas import PreprocessOptions
    from app.services.raster.tiles import generate_tiles
    from app.services.tile_json import scan_tile_extents

    source = write_rgb_geotiff_4326(
        tmp_path / "small.tif",
        width=80,
        height=60,
        west=83.253,
        north=17.712,
        pixel_deg=0.00008,
    )
    preprocessed = preprocess_imagery(
        source,
        tmp_path / "prep",
        PreprocessOptions(target_crs="EPSG:3857", build_overviews=True, add_alpha=True, block_size=32),
        gdal_cachemax=64,
    )
    tiles_dir = tmp_path / "tiles"
    generate_tiles(
        preprocessed,
        tiles_dir,
        TilingOptions(
            profile=TileProfile.MERCATOR,
            tile_scheme=TileScheme.XYZ,
            start_zoom=8,
            end_zoom=0,
            thread_count=1,
        ),
        cache_bytes=64 * 1024 * 1024,
    )
    levels = scan_tile_extents(tiles_dir)
    assert levels
    assert min(levels) == 0
    assert max(levels) == 8
