"""GDAL preprocess command builder tests."""

from pathlib import Path

import pytest

from app.schemas import PreprocessOptions
from app.services.preprocessor import (
    GDAL_BIN,
    build_overview_add_command,
    build_raster_info_command,
    build_reproject_command,
)


def test_build_reproject_command_basic():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_reproject_command(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(target_crs="EPSG:3857"),
    )
    assert cmd[:4] == [GDAL_BIN, "raster", "reproject", "--dst-crs"]
    assert cmd[cmd.index("--dst-crs") + 1] == "EPSG:3857"
    assert "-r" in cmd
    assert cmd[cmd.index("-r") + 1] == "bilinear"
    assert "--overwrite" in cmd
    assert "--co" in cmd
    assert "TILED=YES" in cmd
    assert "BIGTIFF=IF_SAFER" in cmd
    assert "--add-alpha" in cmd
    assert cmd[-2:] == [str(Path("/data/in.tif")), str(Path("/data/warped.tif"))]


def test_build_reproject_command_white_as_transparent():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_reproject_command(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(add_alpha=False, white_as_transparent=True),
    )
    assert "--add-alpha" in cmd
    assert "--src-nodata" in cmd
    assert cmd[cmd.index("--src-nodata") + 1] == "255 255 255"


def test_build_reproject_command_jpeg_compress_override():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_reproject_command(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(compress="JPEG", add_alpha=False),
        compress="DEFLATE",
    )
    assert "COMPRESS=DEFLATE" in cmd


def test_build_raster_info_command_text():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_info_command(Path("/data/in.tif"))
    assert cmd == [GDAL_BIN, "raster", "info", str(Path("/data/in.tif"))]


def test_build_raster_info_command_json():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_raster_info_command(Path("/data/in.tif"), output_format="json")
    assert cmd == [GDAL_BIN, "raster", "info", "--format", "JSON", str(Path("/data/in.tif"))]


def test_build_overview_add_command():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_overview_add_command(Path("/data/warped.tif"))
    assert cmd[:5] == [GDAL_BIN, "raster", "overview", "add", "-r"]
    assert cmd[cmd.index("-r") + 1] == "average"
    assert "--levels=2,4,8,16" in cmd
    assert cmd[-1] == str(Path("/data/warped.tif"))
    assert "--progress" not in cmd


def test_build_reproject_command_show_progress():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_reproject_command(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(),
        show_progress=True,
    )
    assert "--progress" in cmd


def test_build_overview_add_command_show_progress():
    if GDAL_BIN is None:
        pytest.skip("gdal CLI not installed")

    cmd = build_overview_add_command(Path("/data/warped.tif"), show_progress=True)
    assert "--progress" in cmd
    assert cmd[-2:] == ["--progress", str(Path("/data/warped.tif"))]
