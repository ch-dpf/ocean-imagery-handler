"""Byte-budget job progress tests."""

from pathlib import Path

from app.schemas import PreprocessOptions, TileProfile, TilingOptions
from app.services.byte_progress import (
    ByteBudget,
    fraction_to_bytes,
    overview_bytes,
    plan_pipeline_bytes,
    raster_bytes,
)
from app.services.job_progress import JobProgressTracker
from tests.raster_fixtures import write_rgb_geotiff_4326


def test_fraction_to_bytes_is_exact_at_100():
    assert fraction_to_bytes(1000, 0.0) == 0
    assert fraction_to_bytes(1000, 50.0) == 500
    assert fraction_to_bytes(1000, 100.0) == 1000
    assert fraction_to_bytes(0, 50.0) == 0


def test_overview_bytes_are_exact_level_sums():
    # 64x64x3; levels 2 and 4 → 32x32x3 + 16x16x3
    assert overview_bytes(64, 64, 3, levels=(2, 4)) == 32 * 32 * 3 + 16 * 16 * 3


def test_plan_pipeline_bytes_counts_reproject_overviews_and_tiles(tmp_path: Path):
    source = write_rgb_geotiff_4326(tmp_path / "src.tif", width=32, height=32)
    budget = plan_pipeline_bytes(
        source,
        PreprocessOptions(
            target_crs="EPSG:3857",
            build_overviews=True,
            add_alpha=True,
            block_size=16,
        ),
        TilingOptions(
            profile=TileProfile.MERCATOR,
            start_zoom=3,
            end_zoom=3,
            thread_count=1,
        ),
        cache_bytes=32 * 1024 * 1024,
    )
    assert budget.reproject > 0
    assert budget.overviews > 0
    assert budget.tiles > 0
    assert budget.total == budget.reproject + budget.overviews + budget.tiles
    assert budget.preprocess == budget.reproject + budget.overviews


def test_job_percent_equals_bytes_done_over_planned():
    budget = ByteBudget(reproject=100, overviews=50, tiles=50)
    tracker = JobProgressTracker(bytes_planned=budget.total)
    tracker.set_bytes_done(budget.reproject)
    assert tracker.snapshot().percent == 50.0
    tracker.set_bytes_done(budget.preprocess)
    assert tracker.snapshot().percent == 75.0
    tracker.set_bytes_done(budget.total)
    assert tracker.snapshot().percent == 100.0
    assert raster_bytes(10, 10, 4) == 400
