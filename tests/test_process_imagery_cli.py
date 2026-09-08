"""Tests for the synchronous real-file imagery CLI."""

import json
from pathlib import Path
from unittest.mock import Mock

from app.config import Settings
from app.schemas import ImageryJobCreate, TileProfile, TileScheme
from scripts.process_imagery import build_parser, process_request, request_from_args


def test_cli_defaults_match_api_request_model():
    args = build_parser().parse_args([])
    request = request_from_args(args)
    assert request.model_dump() == ImageryJobCreate().model_dump()


def test_cli_supports_every_api_request_field():
    args = build_parser().parse_args(
        [
            "--input-path",
            "/data/source.tif",
            "--target-crs",
            "EPSG:3395",
            "--no-build-overviews",
            "--block-size",
            "512",
            "--compress",
            "JPEG",
            "--jpeg-quality",
            "90",
            "--no-add-alpha",
            "--white-as-transparent",
            "--near-white",
            "3",
            "--profile",
            "geodetic",
            "--tile-format",
            "WEBP",
            "--tile-size",
            "512",
            "--start-zoom",
            "12",
            "--end-zoom",
            "2",
            "--resampling-method",
            "lanczos",
            "--thread-count",
            "3",
            "--resume",
            "--verbose",
            "--kml",
            "--tile-scheme",
            "tms",
            "--auto-publish",
            "--tileset-name",
            "coast",
        ]
    )
    request = request_from_args(args)

    assert request.input_path == "/data/source.tif"
    assert request.preprocess.model_dump() == {
        "target_crs": "EPSG:3395",
        "build_overviews": False,
        "block_size": 512,
        "compress": "JPEG",
        "jpeg_quality": 90,
        "add_alpha": False,
        "white_as_transparent": True,
        "near_white": 3,
    }
    assert request.tiling_options.profile is TileProfile.GEODETIC
    assert request.tiling_options.tile_format.value == "WEBP"
    assert request.tiling_options.tile_size == 512
    assert request.tiling_options.start_zoom == 12
    assert request.tiling_options.end_zoom == 2
    assert request.tiling_options.resampling_method.value == "lanczos"
    assert request.tiling_options.thread_count == 3
    assert request.tiling_options.resume is True
    assert request.tiling_options.verbose is True
    assert request.tiling_options.kml is True
    assert request.tiling_options.tile_scheme is TileScheme.TMS
    assert request.publish.auto_publish is True
    assert request.publish.tileset_name == "coast"


def test_request_json_uses_api_shape_and_cli_overrides(tmp_path):
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "input_path": "/data/from-json.tif",
                "preprocess": {"block_size": 512},
                "tiling_options": {"profile": "raster", "end_zoom": 1},
                "publish": {"auto_publish": False},
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--request-json",
            str(request_file),
            "--input-path",
            "/data/override.tif",
            "--profile",
            "mercator",
            "--auto-publish",
        ]
    )
    request = request_from_args(args)

    assert request.input_path == "/data/override.tif"
    assert request.preprocess.block_size == 512
    assert request.tiling_options.profile is TileProfile.MERCATOR
    assert request.tiling_options.end_zoom == 1
    assert request.publish.auto_publish is True


def test_process_request_runs_all_stages_and_resolves_settings(tmp_path, monkeypatch):
    source = tmp_path / "source.tif"
    source.write_bytes(b"source")
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        gdal_cachemax=256,
        tiling_thread_count=5,
        tiling_resume=True,
        auto_publish=True,
        imagery_server_public_url="http://imagery.example",
    )
    preprocessed = Mock()
    bounds = Mock(return_value=[120.0, 30.0, 121.0, 31.0])
    tile = Mock()
    publish = Mock(
        return_value=(
            "http://imagery.example/imagery/coast",
            "coast",
            "http://imagery.example/imagery/coast/{z}/{x}/{y}.png",
        )
    )
    monkeypatch.setattr("scripts.process_imagery.preprocess_imagery", preprocessed)
    monkeypatch.setattr("scripts.process_imagery.parse_wgs84_bounds", bounds)
    monkeypatch.setattr("scripts.process_imagery.run_raster_tile", tile)
    monkeypatch.setattr("scripts.process_imagery.publish_tileset", publish)

    request = ImageryJobCreate.model_validate(
        {"input_path": str(source), "publish": {"tileset_name": "coast"}}
    )
    result = process_request(request, settings=settings, quiet=True)

    assert result["status"] == "completed"
    assert result["published"] is True
    assert result["tileset_name"] == "coast"
    preprocessed.assert_called_once()
    bounds.assert_called_once()
    tile.assert_called_once()
    resolved_tiling = tile.call_args.kwargs["options"]
    assert resolved_tiling.thread_count == 5
    assert resolved_tiling.resume is True
    publish.assert_called_once()
    assert Path(result["output_dir"]).parent.name == result["job_id"]
