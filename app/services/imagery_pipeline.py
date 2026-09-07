"""Synchronous imagery processing pipeline independent of Celery and Redis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.schemas import ImageryJobCreate, JobProgress, JobStatus, TilingOptions
from app.services.byte_progress import fraction_to_bytes, plan_pipeline_bytes
from app.services.job_progress import JobProgressTracker, parse_zoom_level
from app.services.preprocessor import (
    parse_wgs84_bounds,
    preprocess_imagery,
    validate_source_imagery,
)
from app.services.tile_publisher import publish_tileset
from app.services.tiler_runner import run_raster_tile


@dataclass(frozen=True)
class PipelineEvent:
    """A progress/state transition emitted by :func:`run_imagery_pipeline`."""

    status: JobStatus
    stage: str
    progress: JobProgress
    fields: dict[str, Any] = field(default_factory=dict)


PipelineEventCallback = Callable[[PipelineEvent], None]


def resolve_tiling_options(tiling: TilingOptions, settings: Settings) -> TilingOptions:
    """Fill thread_count and resume from settings when the request omits them."""
    return tiling.model_copy(
        update={
            "thread_count": (
                tiling.thread_count
                if tiling.thread_count is not None
                else settings.tiling_thread_count
            ),
            "resume": tiling.resume if tiling.resume is not None else settings.tiling_resume,
        }
    )


def should_auto_publish(request: ImageryJobCreate, settings: Settings) -> bool:
    if request.publish.auto_publish is not None:
        return request.publish.auto_publish
    return settings.auto_publish


def publish_pipeline_tileset(
    job_id: str,
    output_dir: Path,
    request: ImageryJobCreate,
    settings: Settings,
    bounds_wgs84: list[float],
) -> tuple[str, str, str]:
    """Publish a pipeline output without requiring job-store metadata."""
    return publish_tileset(
        job_id=job_id,
        tiles_dir=output_dir,
        tilesets_dir=settings.tilesets_dir,
        public_url=settings.imagery_server_public_url,
        base_path=settings.imagery_base_path,
        profile=request.tiling_options.profile,
        tile_format=request.tiling_options.tile_format,
        bounds_wgs84=bounds_wgs84,
        tileset_name=request.publish.tileset_name,
        tile_scheme=request.tiling_options.tile_scheme,
    )


def run_imagery_pipeline(
    job_id: str,
    request: ImageryJobCreate,
    *,
    settings: Settings | None = None,
    on_event: PipelineEventCallback | None = None,
) -> dict[str, Any]:
    """Run the complete imagery pipeline synchronously.

    This function performs no queueing and does not read or write Redis. Callers
    may persist :class:`PipelineEvent` values, print them, or ignore them.
    Processing and publishing errors are deliberately allowed to propagate.
    """
    resolved_settings = settings or get_settings()
    if not request.input_path:
        raise ValueError("input_path is required for processing")

    input_path = Path(request.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    job_dir = resolved_settings.jobs_dir / job_id
    preprocess_dir = job_dir / "preprocess"
    output_dir = job_dir / "tiles"
    tiling = resolve_tiling_options(request.tiling_options, resolved_settings)
    cache_bytes = max(int(resolved_settings.gdal_cachemax or 64), 1) * 1024 * 1024
    budget = plan_pipeline_bytes(
        input_path,
        request.preprocess,
        tiling,
        cache_bytes=cache_bytes,
    )
    tracker = JobProgressTracker(bytes_planned=budget.total, weight_source="bytes")

    def emit(
        status: JobStatus,
        stage: str,
        *,
        message: str | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        progress = tracker.set_stage(
            stage,
            message=message,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        if on_event is not None:
            on_event(PipelineEvent(status, stage, progress, fields or {}))

    def emit_bytes(
        status: JobStatus,
        stage: str,
        done: int,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> None:
        progress = tracker.set_bytes_done(
            done,
            message=message,
            current_zoom=current_zoom,
        )
        if on_event is not None:
            on_event(PipelineEvent(status, stage, progress))

    emit(JobStatus.RUNNING, "initializing", message="Initializing job")
    emit(JobStatus.RUNNING, "validate_source", message="Validating GeoTIFF metadata")
    source_info = validate_source_imagery(
        input_path,
        gdal_cachemax=resolved_settings.gdal_cachemax,
        target_crs=request.preprocess.target_crs,
    )
    bounds_wgs84 = source_info["wgs84Bounds"]
    emit(
        JobStatus.RUNNING,
        "validate_source",
        message="GeoTIFF metadata validated",
        fields={"bounds_wgs84": bounds_wgs84},
    )

    emit(
        JobStatus.PREPROCESSING,
        "gdal_preprocess",
        message="Running raster preprocess",
    )

    def preprocess_progress(sub_percent: float, message: str | None) -> None:
        emit_bytes(
            JobStatus.PREPROCESSING,
            "gdal_preprocess",
            fraction_to_bytes(budget.preprocess, sub_percent),
            message=message,
        )

    preprocessed = preprocess_imagery(
        input_path=input_path,
        work_dir=preprocess_dir,
        options=request.preprocess,
        gdal_cachemax=resolved_settings.gdal_cachemax,
        on_subprogress=preprocess_progress,
    )

    bounds_wgs84 = parse_wgs84_bounds(
        preprocessed,
        env={"GDAL_CACHEMAX": str(resolved_settings.gdal_cachemax)},
    )
    emit(
        JobStatus.PREPROCESSING,
        "gdal_preprocess",
        message="Raster preprocess completed",
        fields={"bounds_wgs84": bounds_wgs84},
    )

    emit(
        JobStatus.TILING,
        "gdal_raster_tile",
        message="Generating tiles",
        min_zoom=tiling.end_zoom,
        max_zoom=tiling.start_zoom,
    )

    def tile_progress(sub_percent: float, message: str | None) -> None:
        emit_bytes(
            JobStatus.TILING,
            "gdal_raster_tile",
            budget.preprocess + fraction_to_bytes(budget.tiles, sub_percent),
            message=message or "Generating tiles",
            current_zoom=parse_zoom_level(message) if message else None,
        )

    run_raster_tile(
        input_path=preprocessed,
        output_dir=output_dir,
        options=tiling,
        gdal_cachemax=resolved_settings.gdal_cachemax,
        on_subprogress=tile_progress,
    )

    result: dict[str, Any] = {
        "job_id": job_id,
        "status": JobStatus.COMPLETED.value,
        "output_dir": str(output_dir),
        "bounds_wgs84": bounds_wgs84,
        "published": False,
    }

    completion_message = "Completed"
    if should_auto_publish(request, resolved_settings):
        emit(
            JobStatus.PUBLISHING,
            "register_tileset",
            message="Registering tileset",
        )
        imagery_url, tileset_name, url_template = publish_pipeline_tileset(
            job_id,
            output_dir,
            request,
            resolved_settings,
            bounds_wgs84,
        )
        result.update(
            {
                "imagery_url": imagery_url,
                "tileset_name": tileset_name,
                "cesium_url_template": url_template,
                "published": True,
            }
        )
        completion_message = "Completed and published"

    tracker.set_bytes_done(budget.total, message=completion_message)
    emit(
        JobStatus.COMPLETED,
        "done",
        message=completion_message,
        fields=result,
    )
    return result
