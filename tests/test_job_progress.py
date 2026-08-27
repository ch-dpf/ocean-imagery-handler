"""Job progress parsing and mapping tests."""

import subprocess
import sys
import time

import pytest

from app.schemas import JobProgress
from app.services.job_progress import (
    JobProgressTracker,
    ThrottledProgressWriter,
    gdal_progress_flag_unsupported,
    map_subprogress,
    parse_gdal_progress_chunk,
    parse_zoom_level,
    progress_to_store_fields,
    run_gdal_command,
)
from app.services.progress_calibration import build_stage_ranges


def test_parse_gdal_dot_progress():
    current = 0.0
    current = parse_gdal_progress_chunk("0...10...20", current)
    assert current == 20.0
    current = parse_gdal_progress_chunk("...30...40", current)
    assert current == 40.0
    current = parse_gdal_progress_chunk("100 - done.", current)
    assert current == 100.0


def test_parse_gdal_percent_progress():
    current = parse_gdal_progress_chunk("Processing: 42.5%", 0.0)
    assert current == 42.5


def test_parse_zoom_level():
    assert parse_zoom_level("Generating overview for zoom level 12") == 12
    assert parse_zoom_level("zoom=8") == 8
    assert parse_zoom_level("no zoom here") is None


def test_map_subprogress_within_stage():
    default_ranges = {
        "gdal_preprocess": (2.0, 25.0),
        "gdal_raster_tile": (25.0, 95.0),
    }
    assert map_subprogress("gdal_preprocess", 0.0, default_ranges) == 2.0
    assert map_subprogress("gdal_preprocess", 100.0, default_ranges) == 25.0
    assert map_subprogress("gdal_raster_tile", 50.0, default_ranges) == 60.0


def test_map_subprogress_with_calibrated_ranges():
    calibrated = build_stage_ranges(
        {
            "initializing": 0.01,
            "gdal_preprocess": 0.09,
            "gdal_raster_tile": 0.85,
            "register_tileset": 0.05,
        }
    )
    assert map_subprogress("gdal_raster_tile", 50.0, calibrated) == 52.5


def test_tracker_monotonic_subprogress():
    tracker = JobProgressTracker(stage="gdal_raster_tile", min_zoom=0, max_zoom=10)
    first = tracker.update_subprogress(10.0, message="Starting")
    second = tracker.update_subprogress(5.0, message="Should not regress")
    assert second.percent >= first.percent


def test_tracker_zoom_assists_subprogress():
    tracker = JobProgressTracker(stage="gdal_raster_tile", min_zoom=0, max_zoom=10)
    progress = tracker.update_subprogress(0.0, current_zoom=5)
    assert progress.percent > 25.0
    assert progress.current_zoom == 5


def test_throttled_writer_forces_final_emit():
    emitted: list[JobProgress] = []

    writer = ThrottledProgressWriter(
        lambda progress: emitted.append(progress),
        min_interval_seconds=60.0,
        min_percent_delta=50.0,
    )
    progress = JobProgress(percent=1.0, phase="gdal_preprocess", message="a")
    writer.emit(progress)
    writer.emit(progress)
    writer.emit(progress, force=True)
    assert len(emitted) == 2


def test_gdal_progress_flag_unsupported():
    stderr = "ERROR 1: Unknown argument: -progress\nUsage: gdalwarp ..."
    assert gdal_progress_flag_unsupported(stderr) is True
    assert gdal_progress_flag_unsupported("gdalwarp failed: out of memory") is False


def test_tracker_exposes_weight_source():
    tracker = JobProgressTracker(
        stage="gdal_preprocess",
        stage_ranges={"gdal_preprocess": (0.0, 10.0), "done": (100.0, 100.0), "failed": (0.0, 100.0)},
        weight_source="historical",
        calibration_samples=12,
    )
    progress = tracker.update_subprogress(50.0, message="Halfway")
    assert progress.weight_source == "historical"
    assert progress.calibration_samples == 12
    assert progress.percent == 5.0


def test_progress_to_store_fields():
    payload = progress_to_store_fields(
        JobProgress(percent=33.3, phase="gdal_raster_tile", message="Generating tiles")
    )
    assert payload["progress"]["percent"] == 33.3
    assert payload["progress"]["phase"] == "gdal_raster_tile"


def test_run_gdal_command_streams_stdout_progress_before_exit():
    """Progress dots arrive mid-run; must not wait for process EOF (GDAL tile behavior)."""
    script = (
        "import sys, time\n"
        "parts = ['0...', '10...', '20...', '30...', '40...', "
        "'50...', '60...', '70...', '80...', '90...', '100 - done.\\n']\n"
        "for part in parts:\n"
        "    sys.stdout.write(part)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.12)\n"
    )
    events: list[tuple[float, float]] = []
    started = time.monotonic()

    def on_subprogress(percent: float, _message: str | None) -> None:
        events.append((time.monotonic() - started, percent))

    run_gdal_command([sys.executable, "-c", script], on_subprogress=on_subprogress)

    mid = [(elapsed, percent) for elapsed, percent in events if 0.0 < percent < 100.0]
    assert mid, f"expected mid-run progress updates, got {events!r}"
    # Writer sleeps ~1.3s total; first real step should arrive early, not only at EOF.
    assert mid[0][0] < 0.8
    assert any(percent >= 20.0 for _, percent in mid)
    assert events[-1][1] == 100.0


def test_run_gdal_command_failure_preserves_stderr_text():
    script = "import sys; sys.stderr.write('ERROR 1: boom\\n'); sys.exit(2)\n"
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_gdal_command([sys.executable, "-c", script])
    assert "ERROR 1: boom" in (exc_info.value.stderr or "")
    assert isinstance(exc_info.value.stderr, str)
