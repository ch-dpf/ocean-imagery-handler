"""Tests for the in-process GDAL raster tile runner."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.schemas import ResamplingMethod, TileProfile, TileScheme, TilingOptions
from app.services.gdal_runtime import GdalPythonError
from app.services.tiler_runner import (
    TilerError,
    build_raster_tile_arguments,
    run_raster_tile,
)


def test_build_raster_tile_arguments_zoom_range():
    args = build_raster_tile_arguments(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, start_zoom=18, end_zoom=0, thread_count=4),
    )

    assert args["input"] == str(Path("/data/input.tif"))
    assert args["output"] == str(Path("/data/output"))
    assert args["tiling-scheme"] == "WebMercatorQuad"
    assert args["min-zoom"] == 0
    assert args["max-zoom"] == 18
    assert args["output-format"] == "PNG"
    assert args["convention"] == "xyz"
    assert args["num-threads"] == "4"
    assert args["webviewer"] == ["none"]


def test_build_raster_tile_arguments_tms_convention():
    args = build_raster_tile_arguments(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, tile_scheme=TileScheme.TMS),
    )
    assert args["convention"] == "tms"


def test_build_raster_tile_arguments_auto_zoom():
    args = build_raster_tile_arguments(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(profile=TileProfile.MERCATOR, end_zoom=10),
    )
    assert args["min-zoom"] == 10
    assert "max-zoom" not in args


def test_build_raster_tile_arguments_geodetic_and_resampling_map():
    args = build_raster_tile_arguments(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(
            profile=TileProfile.GEODETIC,
            resampling_method=ResamplingMethod.NEAREST,
            resume=True,
            verbose=True,
        ),
    )
    assert args["tiling-scheme"] == "WorldCRS84Quad"
    assert args["resampling"] == "nearest"
    assert args["resume"] is True


def test_build_raster_tile_arguments_antialias_maps_to_lanczos():
    args = build_raster_tile_arguments(
        Path("/data/input.tif"),
        Path("/data/output"),
        TilingOptions(resampling_method=ResamplingMethod.ANTIALIAS),
    )
    assert args["resampling"] == "lanczos"


def test_run_raster_tile_uses_python_algorithm(tmp_path, monkeypatch):
    algorithm = Mock()
    run = Mock(return_value=algorithm)
    monkeypatch.setattr("app.services.tiler_runner.run_algorithm", run)
    progress = Mock()

    output_dir = tmp_path / "tiles"
    run_raster_tile(
        tmp_path / "input.tif",
        output_dir,
        TilingOptions(start_zoom=2, end_zoom=0),
        512,
        on_subprogress=progress,
    )

    assert output_dir.is_dir()
    assert run.call_args.args[0] == ["raster", "tile"]
    assert run.call_args.kwargs["config"] == {"GDAL_CACHEMAX": "512"}
    assert run.call_args.kwargs["on_progress"] is progress
    algorithm.Finalize.assert_called_once()
    progress.assert_called_with(100.0, "Tiling complete")


def test_run_raster_tile_maps_gdal_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.tiler_runner.run_algorithm",
        Mock(side_effect=GdalPythonError("tile failed")),
    )
    with pytest.raises(TilerError, match="tile failed"):
        run_raster_tile(
            tmp_path / "input.tif",
            tmp_path / "tiles",
            TilingOptions(),
            512,
        )
