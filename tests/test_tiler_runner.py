"""gdal2tiles command builder tests."""

from pathlib import Path

import pytest

from app.schemas import TileProfile, TilingOptions
from app.services.tiler_runner import build_gdal2tiles_command, GDAL2TILES_BIN


def test_build_gdal2tiles_command_zoom_range():
    if GDAL2TILES_BIN is None:
        pytest.skip("gdal2tiles not installed")

    cmd = build_gdal2tiles_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, start_zoom=18, end_zoom=0, thread_count=4),
    )
    assert "--zoom" in cmd
    zoom_idx = cmd.index("--zoom")
    assert cmd[zoom_idx + 1] == "0-18"
    assert "-p" in cmd
    assert "mercator" in cmd
    assert "--tiledriver" in cmd
    assert "PNG" in cmd
    assert "--xyz" in cmd


def test_build_gdal2tiles_command_auto_zoom():
    if GDAL2TILES_BIN is None:
        pytest.skip("gdal2tiles not installed")

    cmd = build_gdal2tiles_command(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR),
    )
    assert "--zoom" not in cmd
