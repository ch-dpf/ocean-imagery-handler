"""Job progress tracking and GDAL subprocess progress parsing."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.schemas import JobProgress

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

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_DOT_STEP_RE = re.compile(r"(?:^|\.{3})(\d+)(?=\.{3}|$|\s|-)")
_DONE_RE = re.compile(r"\b100\b.*\bdone\b", re.IGNORECASE)
_ZOOM_RE = re.compile(
    r"(?:zoom(?:\s+level)?|overview(?:\s+for)?(?:\s+zoom(?:\s+level)?)?)"
    r"[\s:=\-]*(\d+)",
    re.IGNORECASE,
)


def parse_gdal_progress_chunk(chunk: str, current: float) -> float:
    """Parse GDAL classic/progress-bar output and return monotonic 0-100 percent."""
    updated = current

    for match in _DOT_STEP_RE.finditer(chunk):
        updated = max(updated, float(match.group(1)))

    for match in _PERCENT_RE.finditer(chunk):
        updated = max(updated, float(match.group(1)))

    if _DONE_RE.search(chunk) or "100 - done" in chunk.lower():
        updated = 100.0

    return min(max(updated, 0.0), 100.0)


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


def gdal_progress_flag_unsupported(stderr: str) -> bool:
    """True when GDAL rejected a -progress/--progress CLI flag."""
    lowered = stderr.lower()
    return (
        "unknown argument: -progress" in lowered
        or "unknown argument: --progress" in lowered
        or "unknown option" in lowered and "progress" in lowered
    )


def run_gdal_command(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> None:
    """Run a GDAL CLI command, streaming output for tools that support --progress."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    assert process.stderr is not None
    assert process.stdout is not None

    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    sub_percent = 0.0
    stderr_buffer = ""
    lock = threading.Lock()

    def _consume_stderr() -> None:
        nonlocal sub_percent, stderr_buffer
        for chunk in iter(process.stderr.read, ""):
            if not chunk:
                break
            with lock:
                stderr_lines.append(chunk)
                stderr_buffer += chunk
                sub_percent = parse_gdal_progress_chunk(stderr_buffer, sub_percent)
                stderr_buffer = stderr_buffer[-512:]
                message = chunk.strip() or None
                zoom = parse_zoom_level(chunk)
                if zoom is not None and message is None:
                    message = f"Zoom {zoom}"
            if on_subprogress is not None:
                on_subprogress(sub_percent, message if message else None)

    def _consume_stdout() -> None:
        for chunk in iter(process.stdout.read, ""):
            if not chunk:
                break
            with lock:
                stdout_lines.append(chunk)
                sub_percent = parse_gdal_progress_chunk(chunk, sub_percent)
                message = chunk.strip() or None
                zoom = parse_zoom_level(chunk)
                if zoom is not None and message is None:
                    message = f"Zoom {zoom}"
            if on_subprogress is not None:
                on_subprogress(sub_percent, message if message else None)

    stderr_thread = threading.Thread(target=_consume_stderr, daemon=True)
    stdout_thread = threading.Thread(target=_consume_stdout, daemon=True)
    stderr_thread.start()
    stdout_thread.start()

    return_code = process.wait()
    stderr_thread.join(timeout=5)
    stdout_thread.join(timeout=5)

    if on_subprogress is not None and return_code == 0:
        on_subprogress(100.0, None)

    if return_code != 0:
        stderr = "".join(stderr_lines)
        stdout = "".join(stdout_lines)
        raise subprocess.CalledProcessError(
            return_code,
            cmd,
            output=stdout,
            stderr=stderr,
        )


def progress_to_store_fields(progress: JobProgress) -> dict[str, Any]:
    """Serialize JobProgress for Redis job metadata."""
    return {"progress": progress.model_dump()}
