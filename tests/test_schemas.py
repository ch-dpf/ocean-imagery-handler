"""Schema validation tests."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import ImageryJobCreate, PreprocessOptions, TileScheme, TilingOptions
from app.worker.tasks import _resolve_tiling_options


def test_default_job_request():
    request = ImageryJobCreate(input_path="/data/workspace/ortho.tif")
    assert request.preprocess.target_crs == "EPSG:3857"
    assert request.preprocess.block_size == 256
    assert request.tiling_options.profile.value == "mercator"
    assert request.tiling_options.tile_scheme == TileScheme.XYZ
    assert request.tiling_options.thread_count is None
    assert request.tiling_options.resume is None


def test_tiling_options_tile_scheme_tms():
    options = TilingOptions(tile_scheme=TileScheme.TMS)
    assert options.tile_scheme == TileScheme.TMS
    assert options.model_dump()["tile_scheme"] == "tms"


def test_tiling_options_serialization():
    options = TilingOptions(start_zoom=18, end_zoom=0, resume=True)
    payload = options.model_dump()
    assert payload["start_zoom"] == 18
    assert payload["resume"] is True


def test_resolve_tiling_options_uses_settings_defaults():
    settings = Settings(tiling_thread_count=8, tiling_resume=True)
    resolved = _resolve_tiling_options(TilingOptions(), settings)
    assert resolved.thread_count == 8
    assert resolved.resume is True


def test_resolve_tiling_options_keeps_explicit_request_values():
    settings = Settings(tiling_thread_count=8, tiling_resume=True)
    resolved = _resolve_tiling_options(
        TilingOptions(thread_count=2, resume=False),
        settings,
    )
    assert resolved.thread_count == 2
    assert resolved.resume is False


def test_block_size_rejects_non_multiple_of_16():
    with pytest.raises(ValidationError):
        PreprocessOptions(block_size=65)
