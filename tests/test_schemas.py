"""Schema validation tests."""

import pytest
from pydantic import ValidationError

from app.schemas import ImageryJobCreate, PreprocessOptions, TileScheme, TilingOptions


def test_default_job_request():
    request = ImageryJobCreate(input_path="/data/workspace/ortho.tif")
    assert request.preprocess.target_crs == "EPSG:3857"
    assert request.preprocess.block_size == 256
    assert request.tiling_options.profile.value == "mercator"
    assert request.tiling_options.tile_scheme == TileScheme.XYZ


def test_tiling_options_tile_scheme_tms():
    options = TilingOptions(tile_scheme=TileScheme.TMS)
    assert options.tile_scheme == TileScheme.TMS
    assert options.model_dump()["tile_scheme"] == "tms"


def test_tiling_options_serialization():
    options = TilingOptions(start_zoom=18, end_zoom=0, resume=True)
    payload = options.model_dump()
    assert payload["start_zoom"] == 18
    assert payload["resume"] is True


def test_block_size_rejects_non_multiple_of_16():
    with pytest.raises(ValidationError):
        PreprocessOptions(block_size=65)
