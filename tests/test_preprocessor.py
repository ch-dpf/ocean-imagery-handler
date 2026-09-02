"""Python raster preprocess tests (no GDAL CLI)."""

from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.preprocessor import gdal_info, parse_wgs84_bounds, preprocess_imagery
from app.services.raster.geotiff import GeoTiffReader
from tests.raster_fixtures import write_rgb_geotiff_4326


def test_parse_wgs84_bounds_from_epsg_4326(tmp_path: Path):
    dataset = write_rgb_geotiff_4326(
        tmp_path / "src.tif",
        width=50,
        height=40,
        west=10.0,
        north=50.0,
        pixel_deg=0.01,
    )
    west, south, east, north = parse_wgs84_bounds(dataset)
    assert west == 10.0
    assert north == 50.0
    assert abs(east - (10.0 + 50 * 0.01)) < 1e-6
    assert abs(south - (50.0 - 40 * 0.01)) < 1e-6


def test_gdal_info_text_contains_size(tmp_path: Path):
    dataset = write_rgb_geotiff_4326(tmp_path / "src.tif", width=32, height=16)
    text = gdal_info(dataset)
    assert "Size is 32, 16" in text
    assert "EPSG:4326" in text


def test_preprocess_reprojects_to_3857_and_adds_alpha(tmp_path: Path):
    source = write_rgb_geotiff_4326(tmp_path / "src.tif", width=32, height=32)
    work = tmp_path / "work"
    output = preprocess_imagery(
        source,
        work,
        PreprocessOptions(
            target_crs="EPSG:3857",
            build_overviews=True,
            add_alpha=True,
            block_size=32,
        ),
        gdal_cachemax=64,
    )
    assert output.name == "preprocessed.tif"
    assert not (work / "warped.tif").exists()
    assert output.is_file()
    with GeoTiffReader(output) as src:
        assert src.crs.to_epsg() == 3857
        assert src.samples == 4
    assert Path(str(output) + ".ovr").is_file()


def test_white_as_transparent_sets_alpha_zero(tmp_path: Path):
    source = write_rgb_geotiff_4326(tmp_path / "src.tif", width=16, height=16)
    work = tmp_path / "work"
    output = preprocess_imagery(
        source,
        work,
        PreprocessOptions(
            target_crs="EPSG:3857",
            build_overviews=False,
            add_alpha=False,
            white_as_transparent=True,
            block_size=16,
        ),
        gdal_cachemax=64,
    )
    with GeoTiffReader(output) as src:
        assert src.samples == 4
        window = src.read_window(0, 0, min(8, src.height), min(8, src.width))
        # Reprojected white fill outside footprint should be transparent.
        assert window.shape[2] == 4
