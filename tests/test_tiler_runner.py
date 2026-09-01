"""Python raster tile tests (no GDAL CLI)."""

from pathlib import Path

from app.schemas import ResamplingMethod, TileProfile, TileScheme, TilingOptions
from app.services.preprocessor import preprocess_imagery
from app.schemas import PreprocessOptions
from app.services.tiler_runner import PROFILE_TO_TILING_SCHEME, RESAMPLING_TO_GDAL, run_raster_tile
from tests.raster_fixtures import write_rgb_geotiff_4326


def test_resampling_and_profile_maps():
    assert PROFILE_TO_TILING_SCHEME[TileProfile.MERCATOR] == "WebMercatorQuad"
    assert PROFILE_TO_TILING_SCHEME[TileProfile.GEODETIC] == "WorldCRS84Quad"
    assert RESAMPLING_TO_GDAL[ResamplingMethod.ANTIALIAS] == "lanczos"
    assert RESAMPLING_TO_GDAL[ResamplingMethod.NEAREST] == "nearest"


def test_run_raster_tile_mercator_xyz(tmp_path: Path):
    source = write_rgb_geotiff_4326(
        tmp_path / "src.tif",
        width=64,
        height=64,
        west=116.3,
        north=39.95,
        pixel_deg=0.0005,
    )
    preprocessed = preprocess_imagery(
        source,
        tmp_path / "prep",
        PreprocessOptions(target_crs="EPSG:3857", build_overviews=False, add_alpha=True, block_size=32),
        gdal_cachemax=64,
    )
    tiles_dir = tmp_path / "tiles"
    run_raster_tile(
        preprocessed,
        tiles_dir,
        TilingOptions(
            profile=TileProfile.MERCATOR,
            tile_scheme=TileScheme.XYZ,
            start_zoom=12,
            end_zoom=11,
            thread_count=1,
        ),
        gdal_cachemax=64,
    )
    zoom_dirs = [p for p in tiles_dir.iterdir() if p.is_dir() and p.name.isdigit()]
    assert {p.name for p in zoom_dirs} >= {"11", "12"}
    pngs = list(tiles_dir.rglob("*.png"))
    assert pngs
    assert all(p.stat().st_size > 0 for p in pngs)


def test_run_raster_tile_tms_and_kml(tmp_path: Path):
    source = write_rgb_geotiff_4326(tmp_path / "src.tif", width=48, height=48)
    preprocessed = preprocess_imagery(
        source,
        tmp_path / "prep",
        PreprocessOptions(target_crs="EPSG:3857", build_overviews=False, block_size=16),
        gdal_cachemax=32,
    )
    tiles_dir = tmp_path / "tiles"
    run_raster_tile(
        preprocessed,
        tiles_dir,
        TilingOptions(
            profile=TileProfile.MERCATOR,
            tile_scheme=TileScheme.TMS,
            start_zoom=10,
            end_zoom=10,
            kml=True,
            thread_count=1,
        ),
        gdal_cachemax=32,
    )
    assert (tiles_dir / "doc.kml").is_file()
    assert list((tiles_dir / "10").rglob("*.png"))
