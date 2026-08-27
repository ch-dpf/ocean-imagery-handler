"""gdal raster tile command builder tests."""

from pathlib import Path

import pytest

from app.schemas import ResamplingMethod, TileProfile, TileScheme, TilingOptions
from app.services.tiler_runner import GDAL_BIN, build_raster_tile_command


def test_build_raster_tile_command_zoom_range():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, start_zoom=18, end_zoom=0, thread_count=4),
    )
    assert cmd[:3] == [GDAL_BIN, "raster", "tile"]
    assert "--tiling-scheme" in cmd
    assert cmd[cmd.index("--tiling-scheme") + 1] == "WebMercatorQuad"
    assert "--min-zoom" in cmd
    assert cmd[cmd.index("--min-zoom") + 1] == "0"
    assert "--max-zoom" in cmd
    assert cmd[cmd.index("--max-zoom") + 1] == "18"
    assert "--format" in cmd
    assert "PNG" in cmd
    assert "--convention" in cmd
    assert cmd[cmd.index("--convention") + 1] == "xyz"
    assert "-j" in cmd
    assert cmd[cmd.index("-j") + 1] == "4"
    assert "--webviewer" in cmd
    assert cmd[cmd.index("--webviewer") + 1] == "none"
    assert "-q" in cmd


def test_build_raster_tile_command_tms_convention():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, tile_scheme=TileScheme.TMS),
    )
    assert cmd[cmd.index("--convention") + 1] == "tms"


def test_build_raster_tile_command_auto_zoom():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR),
    )
    assert "--min-zoom" in cmd
    assert cmd[cmd.index("--min-zoom") + 1] == "0"
    assert "--max-zoom" not in cmd


def test_build_raster_tile_command_auto_max_with_custom_min_zoom():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, end_zoom=10),
    )
    assert cmd[cmd.index("--min-zoom") + 1] == "10"
    assert "--max-zoom" not in cmd


def test_build_raster_tile_command_geodetic_and_resampling_map():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(
            profile=TileProfile.GEODETIC,
            resampling_method=ResamplingMethod.NEAREST,
            resume=True,
            verbose=True,
        ),
    )
    assert cmd[cmd.index("--tiling-scheme") + 1] == "WorldCRS84Quad"
    assert cmd[cmd.index("-r") + 1] == "nearest"
    assert "--resume" in cmd
    assert "-q" not in cmd


def test_build_raster_tile_command_show_progress():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(),
        show_progress=True,
    )
    assert "--progress" in cmd
    assert "-q" not in cmd


def test_build_raster_tile_command_antialias_maps_to_lanczos():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_tile_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(resampling_method=ResamplingMethod.ANTIALIAS),
    )
    assert cmd[cmd.index("-r") + 1] == "lanczos"
