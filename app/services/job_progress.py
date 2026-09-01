"""Job progress tracking and GDAL subprocess progress parsing."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.schemas import JobProgress, JobStatus

ProgressCallback = Callable[[JobProgress], None]


def map_subprogress(
    stage: str,
    sub_percent: float,
    stage_ranges: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Map a 0-100 sub-step percent into an overall range (tests / leftover CLI)."""
    ranges = stage_ranges or {}
    start, end = ranges.get(stage, (0.0, 100.0))
    clamped = min(max(sub_percent, 0.0), 100.0) / 100.0
    return round(start + (end - start) * clamped, 2)

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


@dataclass
class JobProgressTracker:
    """Tracks overall job progress as uncompressed raster bytes written / planned."""

    stage: str = "queued"
    message: str | None = None
    current_zoom: int | None = None
    min_zoom: int | None = None
    max_zoom: int | None = None
    bytes_planned: int = 0
    bytes_done: int = 0
    weight_source: str = "bytes"

    def set_stage(
        self,
        stage: str,
        *,
        message: str | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> JobProgress:
        self.stage = stage
        if message is not None:
            self.message = message
        if min_zoom is not None:
            self.min_zoom = min_zoom
        if max_zoom is not None:
            self.max_zoom = max_zoom
        if stage != "gdal_raster_tile":
            self.current_zoom = None
        return self.snapshot()

    def set_bytes_done(
        self,
        done: int,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> JobProgress:
        planned = max(int(self.bytes_planned), 0)
        value = max(0, int(done))
        if planned > 0:
            value = min(value, planned)
        self.bytes_done = max(self.bytes_done, value)
        if message is not None:
            self.message = message
        if current_zoom is not None:
            self.current_zoom = current_zoom
        return self.snapshot()

    def snapshot(self) -> JobProgress:
        if self.stage == "done":
            percent = 100.0
        elif self.bytes_planned <= 0:
            percent = 0.0
        else:
            percent = round(100.0 * self.bytes_done / self.bytes_planned, 2)
            percent = min(max(percent, 0.0), 100.0)
        if self.stage == "failed":
            percent = min(percent, 99.99)

        return JobProgress(
            percent=percent,
            phase=self.stage,
            message=self.message,
            current_zoom=self.current_zoom,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            weight_source=self.weight_source,
            bytes_done=self.bytes_done,
            bytes_planned=self.bytes_planned,
            calibration_samples=None,
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


# GDAL --progress often emits "0...10...20..." without newlines until completion.
# read()-until-EOF would hide mid-run updates; small binary chunks keep parsing live.
_STREAM_CHUNK_SIZE = 256
_PARSE_WINDOW = 512


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
        text=False,
        env=env,
        bufsize=0,
    )
    assert process.stderr is not None
    assert process.stdout is not None

    stderr_chunks: list[str] = []
    stdout_chunks: list[str] = []
    sub_percent = 0.0
    lock = threading.Lock()

    def _consume_stream(stream: Any, collected: list[str]) -> None:
        nonlocal sub_percent
        parse_buffer = ""
        while True:
            raw = stream.read(_STREAM_CHUNK_SIZE)
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace")
            message: str | None = None
            with lock:
                collected.append(text)
                parse_buffer = (parse_buffer + text)[-_PARSE_WINDOW:]
                sub_percent = parse_gdal_progress_chunk(parse_buffer, sub_percent)
                message = text.strip() or None
                zoom = parse_zoom_level(text)
                if zoom is not None and message is None:
                    message = f"Zoom {zoom}"
                percent_snapshot = sub_percent
            if on_subprogress is not None:
                on_subprogress(percent_snapshot, message)

    stderr_thread = threading.Thread(
        target=_consume_stream,
        args=(process.stderr, stderr_chunks),
        daemon=True,
    )
    stdout_thread = threading.Thread(
        target=_consume_stream,
        args=(process.stdout, stdout_chunks),
        daemon=True,
    )
    stderr_thread.start()
    stdout_thread.start()

    return_code = process.wait()
    stderr_thread.join(timeout=5)
    stdout_thread.join(timeout=5)

    if on_subprogress is not None and return_code == 0:
        on_subprogress(100.0, None)

    if return_code != 0:
        stderr = "".join(stderr_chunks)
        stdout = "".join(stdout_chunks)
        raise subprocess.CalledProcessError(
            return_code,
            cmd,
            output=stdout,
            stderr=stderr,
        )


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
