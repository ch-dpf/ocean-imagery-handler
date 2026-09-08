"""Job progress tracking for the imagery processing pipeline."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.schemas import JobProgress, JobStatus

ProgressCallback = Callable[[JobProgress], None]

# Overall pipeline ranges (start_percent, end_percent) keyed by worker stage.
# Used as fallback when historical calibration is unavailable.
DEFAULT_STAGE_RANGES: dict[str, tuple[float, float]] = {
    "queued": (0.0, 0.0),
    "initializing": (0.0, 2.0),
    "gdal_preprocess": (2.0, 25.0),
    "gdal_raster_tile": (25.0, 95.0),
    "register_tileset": (95.0, 99.0),
    "done": (100.0, 100.0),
    "failed": (0.0, 100.0),
}

# Backward-compatible alias for tests and imports.
STAGE_RANGES = DEFAULT_STAGE_RANGES

_ZOOM_RE = re.compile(
    r"(?:zoom(?:\s+level)?|overview(?:\s+for)?(?:\s+zoom(?:\s+level)?)?)"
    r"[\s:=\-]*(\d+)",
    re.IGNORECASE,
)


def parse_zoom_level(chunk: str) -> int | None:
    """Extract zoom level from verbose gdal raster tile log lines."""
    match = _ZOOM_RE.search(chunk)
    if match is None:
        return None
    return int(match.group(1))


def map_subprogress(
    stage: str,
    sub_percent: float,
    stage_ranges: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Map a 0-100 sub-step percent into the overall pipeline percent."""
    ranges = stage_ranges or DEFAULT_STAGE_RANGES
    start, end = ranges.get(stage, (0.0, 100.0))
    clamped = min(max(sub_percent, 0.0), 100.0) / 100.0
    return round(start + (end - start) * clamped, 2)


@dataclass
class JobProgressTracker:
    """Tracks overall job progress across pipeline stages."""

    stage: str = "queued"
    sub_percent: float = 0.0
    message: str | None = None
    current_zoom: int | None = None
    min_zoom: int | None = None
    max_zoom: int | None = None
    stage_ranges: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_STAGE_RANGES))
    weight_source: str = "default"
    calibration_samples: int = 0

    def set_stage(
        self,
        stage: str,
        *,
        message: str | None = None,
        sub_percent: float = 0.0,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> JobProgress:
        self.stage = stage
        self.sub_percent = sub_percent
        if message is not None:
            self.message = message
        if min_zoom is not None:
            self.min_zoom = min_zoom
        if max_zoom is not None:
            self.max_zoom = max_zoom
        if stage != "gdal_raster_tile":
            self.current_zoom = None
        return self.snapshot()

    def update_subprogress(
        self,
        sub_percent: float,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> JobProgress:
        self.sub_percent = min(max(sub_percent, self.sub_percent), 100.0)
        if message is not None:
            self.message = message
        if current_zoom is not None:
            self.current_zoom = current_zoom
            if self.max_zoom is not None and self.min_zoom is not None:
                zoom_span = self.max_zoom - self.min_zoom
                if zoom_span > 0:
                    zoom_ratio = (self.max_zoom - current_zoom) / zoom_span
                    zoom_percent = min(max(zoom_ratio * 100.0, 0.0), 100.0)
                    self.sub_percent = max(self.sub_percent, zoom_percent)
        return self.snapshot()

    def snapshot(self) -> JobProgress:
        percent = map_subprogress(self.stage, self.sub_percent, self.stage_ranges)
        if self.stage == "done":
            percent = 100.0
        elif self.stage == "failed":
            percent = min(percent, 99.99)

        return JobProgress(
            percent=percent,
            phase=self.stage,
            message=self.message,
            current_zoom=self.current_zoom,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            weight_source=self.weight_source,
            calibration_samples=self.calibration_samples if self.calibration_samples > 0 else None,
        )


@dataclass
class ThrottledProgressWriter:
    """Write progress to a callback without flooding the job store."""

    callback: ProgressCallback
    min_interval_seconds: float = 1.0
    min_percent_delta: float = 0.5
    _last_emit_at: float = field(default=0.0, init=False)
    _last_percent: float = field(default=-1.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def emit(self, progress: JobProgress, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_emit_at
            delta = abs(progress.percent - self._last_percent)
            if not force and elapsed < self.min_interval_seconds and delta < self.min_percent_delta:
                return
            self._last_emit_at = now
            self._last_percent = progress.percent
        self.callback(progress)


def progress_to_store_fields(progress: JobProgress) -> dict[str, Any]:
    """Serialize JobProgress for Redis job metadata."""
    return {"progress": progress.model_dump()}


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def compute_elapsed_seconds(
    data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float | None:
    """Compute wall-clock job duration in seconds from stored timestamps."""
    start = _parse_iso_timestamp(data.get("created_at"))
    if start is None:
        return None

    end = _parse_iso_timestamp(data.get("completed_at"))
    if end is None:
        status = data.get("status")
        if status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
            end = _parse_iso_timestamp(data.get("updated_at")) or start
        else:
            end = now or datetime.now(UTC)

    return round(max(0.0, (end - start).total_seconds()), 3)
