"""Tests for the in-process GDAL preprocessing pipeline."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.schemas import PreprocessOptions
from app.services.gdal_runtime import GdalPythonError
from app.services.preprocessor import (
    PreprocessError,
    _bounds_from_wgs84_extent,
    build_overview_add_arguments,
    build_raster_info_arguments,
    build_reproject_arguments,
    parse_wgs84_bounds,
    preprocess_imagery,
)


def test_build_reproject_arguments_basic():
    args = build_reproject_arguments(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(target_crs="EPSG:3857"),
    )

    assert args["input"] == str(Path("/data/in.tif"))
    assert args["output"] == str(Path("/data/warped.tif"))
    assert args["dst-crs"] == "EPSG:3857"
    assert args["resampling"] == "bilinear"
    assert args["overwrite"] is True
    assert args["num-threads"] == "ALL_CPUS"
    assert "TILED=YES" in args["creation-option"]
    assert "BIGTIFF=IF_SAFER" in args["creation-option"]
    assert args["add-alpha"] is True


def test_build_reproject_arguments_white_as_transparent():
    args = build_reproject_arguments(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(add_alpha=False, white_as_transparent=True),
    )

    assert args["add-alpha"] is True
    assert args["src-nodata"] == [255.0, 255.0, 255.0]


def test_build_reproject_arguments_jpeg_compress_override():
    args = build_reproject_arguments(
        Path("/data/in.tif"),
        Path("/data/warped.tif"),
        PreprocessOptions(compress="JPEG", add_alpha=False),
        compress="DEFLATE",
    )
    assert "COMPRESS=DEFLATE" in args["creation-option"]


def test_build_info_and_overview_arguments():
    dataset = Path("/data/warped.tif")
    assert build_raster_info_arguments(dataset, output_format="json") == {
        "input": str(dataset),
        "output-format": "json",
    }
    assert build_overview_add_arguments(dataset) == {
        "input": str(dataset),
        "resampling": "average",
        "levels": [2, 4, 8, 16],
    }


def test_bounds_from_wgs84_extent():
    data = {
        "wgs84Extent": {
            "coordinates": [[[120.0, 31.0], [121.0, 31.0], [121.0, 32.0], [120.0, 31.0]]]
        }
    }
    assert _bounds_from_wgs84_extent(data) == [120.0, 31.0, 121.0, 32.0]


def test_parse_bounds_uses_osr_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.services.preprocessor._raster_info_json",
        lambda *_args, **_kwargs: {
            "coordinateSystem": {"wkt": "PROJCRS[projected]"},
            "cornerCoordinates": {
                "lowerLeft": [0, 0],
                "lowerRight": [1, 0],
                "upperRight": [1, 1],
                "upperLeft": [0, 1],
            },
        },
    )
    monkeypatch.setattr(
        "app.services.preprocessor._transform_points_to_wgs84",
        lambda points, _srs: [(120 + point[0], 30 + point[1]) for point in points],
    )

    assert parse_wgs84_bounds(Path("input.tif")) == [120.0, 30.0, 121.0, 31.0]


def test_preprocess_runs_reproject_then_overview(tmp_path, monkeypatch):
    input_path = tmp_path / "input.tif"
    input_path.write_bytes(b"input")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Algorithm:
        def Finalize(self):
            return None

    def fake_run(path, arguments, **_kwargs):
        calls.append((path, dict(arguments)))
        if path == ["raster", "reproject"]:
            Path(arguments["output"]).write_bytes(b"warped")
        return Algorithm()

    monkeypatch.setattr("app.services.preprocessor.run_algorithm", fake_run)
    progress = Mock()

    result = preprocess_imagery(
        input_path,
        tmp_path / "work",
        PreprocessOptions(build_overviews=True),
        256,
        on_subprogress=progress,
    )

    assert [call[0] for call in calls] == [
        ["raster", "reproject"],
        ["raster", "overview", "add"],
    ]
    assert calls[0][1]["creation-option"]
    assert calls[1][1]["levels"] == [2, 4, 8, 16]
    assert result.read_bytes() == b"warped"
    progress.assert_called_with(100.0, "preprocess complete")


def test_preprocess_maps_gdal_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.preprocessor.run_algorithm",
        Mock(side_effect=GdalPythonError("bad raster")),
    )

    with pytest.raises(PreprocessError, match="bad raster"):
        preprocess_imagery(
            tmp_path / "input.tif",
            tmp_path / "work",
            PreprocessOptions(),
            256,
        )
