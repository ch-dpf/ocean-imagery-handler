"""Celery tasks for imagery processing pipeline."""

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.schemas import ImageryJobCreate, JobProgress, JobStatus, TilingOptions
from app.services.job_progress import (
    JobProgressTracker,
    ThrottledProgressWriter,
    parse_zoom_level,
    progress_to_store_fields,
)
from app.services.progress_calibration import ProgressCalibrationStore
from app.services.tile_json import TileJsonError, _bounds_valid_wgs84
from app.services.job_store import JobStore
from app.services.preprocessor import PreprocessError, parse_wgs84_bounds, preprocess_imagery
from app.services.tile_publisher import PublishError, publish_tileset
from app.services.tiler_runner import TilerError, run_raster_tile
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _store() -> JobStore:
    return JobStore(get_settings())


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _JobProgressReporter:
    def __init__(self, job_id: str, *, auto_publish: bool) -> None:
        self._job_id = job_id
        self._auto_publish = auto_publish
        self._store = _store()
        settings = get_settings()
        calibration = ProgressCalibrationStore(settings, self._store.redis)
        stage_ranges, weight_source, calibration_samples = calibration.get_stage_ranges(
            auto_publish=auto_publish,
        )
        self._calibration = calibration
        self.tracker = JobProgressTracker(
            stage_ranges=stage_ranges,
            weight_source=weight_source,
            calibration_samples=calibration_samples,
        )
        self._writer = ThrottledProgressWriter(self._persist)
        self._current_stage: str | None = None
        self._stage_started_at: float | None = None
        self._stage_durations: dict[str, float] = {}

    def _persist(self, progress: JobProgress) -> None:
        self._store.update(self._job_id, **progress_to_store_fields(progress))

    def _close_current_stage(self) -> None:
        if self._current_stage is None or self._stage_started_at is None:
            return
        elapsed = time.monotonic() - self._stage_started_at
        self._stage_durations[self._current_stage] = elapsed

    def begin_stage(
        self,
        stage: str,
        *,
        status: JobStatus,
        message: str | None = None,
        min_zoom: int | None = None,
        max_zoom: int | None = None,
    ) -> None:
        self._close_current_stage()
        self._current_stage = stage
        self._stage_started_at = time.monotonic()

        progress = self.tracker.set_stage(
            stage,
            message=message,
            sub_percent=0.0,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        self._store.update(
            self._job_id,
            status=status.value,
            stage=stage,
            **progress_to_store_fields(progress),
        )
        self._writer.emit(progress, force=True)

    def update_subprogress(
        self,
        sub_percent: float,
        *,
        message: str | None = None,
        current_zoom: int | None = None,
    ) -> None:
        progress = self.tracker.update_subprogress(
            sub_percent,
            message=message,
            current_zoom=current_zoom,
        )
        self._writer.emit(progress)

    def complete(self, *, message: str = "Done") -> None:
        self._close_current_stage()
        self._calibration.record_job_durations(
            self._stage_durations,
            auto_publish=self._auto_publish,
        )
        progress = self.tracker.set_stage("done", message=message, sub_percent=100.0)
        self._writer.emit(progress, force=True)


def _should_auto_publish(request: ImageryJobCreate, settings) -> bool:
    if request.publish.auto_publish is not None:
        return request.publish.auto_publish
    return settings.auto_publish


def _resolve_tiling_options(tiling: TilingOptions, settings) -> TilingOptions:
    """Fill thread_count / resume from settings when the request omits them."""
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


def _publish_job_tileset(
    job_id: str,
    output_dir: Path,
    request: ImageryJobCreate,
    settings,
    bounds_wgs84: list[float],
    reporter: _JobProgressReporter | None = None,
) -> tuple[str, str, str]:
    store = _store()
    if reporter is not None:
        reporter.begin_stage(
            "register_tileset",
            status=JobStatus.PUBLISHING,
            message="Registering tileset",
        )
    else:
        store.update(job_id, status=JobStatus.PUBLISHING.value, stage="register_tileset")

    imagery_url, tileset_name, url_template = publish_tileset(
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
    return imagery_url, tileset_name, url_template


@celery_app.task(bind=True, name="imagery.process_job")
def process_imagery_job(self, job_id: str, request_data: dict) -> dict:
    settings = get_settings()
    store = _store()
    request = ImageryJobCreate.model_validate(request_data)

    job_dir = settings.jobs_dir / job_id
    preprocess_dir = job_dir / "preprocess"
    output_dir = job_dir / "tiles"
    auto_publish = _should_auto_publish(request, settings)
    reporter = _JobProgressReporter(job_id, auto_publish=auto_publish)

    try:
        reporter.begin_stage(
            "initializing",
            status=JobStatus.RUNNING,
            message="Initializing job",
        )

        if not request.input_path:
            raise ValueError("input_path is required for background processing")

        input_path = Path(request.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        reporter.begin_stage(
            "gdal_preprocess",
            status=JobStatus.PREPROCESSING,
            message="Running raster preprocess",
        )
        preprocessed = preprocess_imagery(
            input_path=input_path,
            work_dir=preprocess_dir,
            options=request.preprocess,
            gdal_cachemax=settings.gdal_cachemax,
            on_subprogress=lambda pct, msg: reporter.update_subprogress(pct, message=msg),
        )

        bounds_wgs84 = parse_wgs84_bounds(preprocessed, env={"GDAL_CACHEMAX": str(settings.gdal_cachemax)})
        store.update(job_id, bounds_wgs84=bounds_wgs84)

        tiling = _resolve_tiling_options(request.tiling_options, settings)
        min_zoom = tiling.end_zoom
        max_zoom = tiling.start_zoom
        reporter.begin_stage(
            "gdal_raster_tile",
            status=JobStatus.TILING,
            message="Generating tiles",
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )

        def _tile_progress(sub_percent: float, message: str | None) -> None:
            current_zoom = parse_zoom_level(message) if message else None
            reporter.update_subprogress(
                sub_percent,
                message=message or "Generating tiles",
                current_zoom=current_zoom,
            )

        run_raster_tile(
            input_path=preprocessed,
            output_dir=output_dir,
            options=tiling,
            gdal_cachemax=settings.gdal_cachemax,
            on_subprogress=_tile_progress,
        )

        result: dict[str, str | bool | list[float]] = {
            "job_id": job_id,
            "status": JobStatus.COMPLETED.value,
            "output_dir": str(output_dir),
            "bounds_wgs84": bounds_wgs84,
            "published": False,
        }

        if auto_publish:
            imagery_url, tileset_name, url_template = _publish_job_tileset(
                job_id, output_dir, request, settings, bounds_wgs84, reporter=reporter
            )
            store.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="done",
                output_dir=str(output_dir),
                imagery_url=imagery_url,
                tileset_name=tileset_name,
                cesium_url_template=url_template,
                published=True,
                error=None,
                completed_at=_utc_now_iso(),
            )
            reporter.complete(message="Completed and published")
            result.update(
                {
                    "imagery_url": imagery_url,
                    "tileset_name": tileset_name,
                    "cesium_url_template": url_template,
                    "published": True,
                }
            )
        else:
            store.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="done",
                output_dir=str(output_dir),
                published=False,
                error=None,
                completed_at=_utc_now_iso(),
            )
            reporter.complete(message="Completed")

        return result

    except (
        PreprocessError,
        TilerError,
        PublishError,
        TileJsonError,
        OSError,
        ValueError,
    ) as exc:
        logger.exception("Job %s failed", job_id)
        reporter._close_current_stage()
        failed_progress = reporter.tracker.snapshot()
        failed_progress.message = str(exc)
        failed_progress.phase = "failed"
        store.update(
            job_id,
            status=JobStatus.FAILED.value,
            stage="failed",
            error=str(exc),
            completed_at=_utc_now_iso(),
            **progress_to_store_fields(failed_progress),
        )
        raise


def publish_completed_job(job_id: str, tileset_name: str | None = None) -> tuple[str, str, str]:
    """Publish tiles for a completed job (manual API).

    Uses Redis metadata when available; otherwise publishes from disk
    at jobs/{job_id}/tiles/ (for expired Redis TTL cases).
    """
    from app.services.tile_publisher import publish_from_disk

    settings = get_settings()
    store = _store()
    data = store.get(job_id)

    if data is None:
        imagery_url, resolved_name, url_template, _tiles_dir = publish_from_disk(
            jobs_dir=settings.jobs_dir,
            workspace_dir=settings.workspace_dir,
            tilesets_dir=settings.tilesets_dir,
            public_url=settings.imagery_server_public_url,
            base_path=settings.imagery_base_path,
            job_id=job_id,
            tileset_name=tileset_name,
            gdal_cachemax=settings.gdal_cachemax,
        )
        return imagery_url, resolved_name, url_template

    status = data.get("status")
    allowed = {JobStatus.COMPLETED.value, JobStatus.PUBLISHING.value}
    if status not in allowed:
        raise ValueError(f"Job is not ready to publish: {status}")

    output_dir = data.get("output_dir")
    if not output_dir:
        raise ValueError("Job has no output_dir")

    bounds_wgs84 = data.get("bounds_wgs84") or [-180.0, -90.0, 180.0, 90.0]
    if not _bounds_valid_wgs84(bounds_wgs84):
        job_dir = settings.jobs_dir / job_id
        preprocessed = job_dir / "preprocess" / "preprocessed.tif"
        if preprocessed.is_file():
            bounds_wgs84 = parse_wgs84_bounds(
                preprocessed,
                env={"GDAL_CACHEMAX": str(settings.gdal_cachemax)},
            )
            store.update(job_id, bounds_wgs84=bounds_wgs84)
            # Force tile.json regeneration with corrected bounds.
            tile_json = Path(output_dir) / "tile.json"
            tile_json.unlink(missing_ok=True)
            (Path(output_dir) / "imagery.json").unlink(missing_ok=True)

    request_data = data.get("request") or {}
    request = ImageryJobCreate.model_validate(request_data)
    if tileset_name is not None:
        request = request.model_copy(
            update={"publish": request.publish.model_copy(update={"tileset_name": tileset_name})}
        )

    imagery_url, resolved_name, url_template = _publish_job_tileset(
        job_id,
        Path(output_dir),
        request,
        settings,
        bounds_wgs84,
    )
    store.update(
        job_id,
        status=JobStatus.COMPLETED.value,
        imagery_url=imagery_url,
        tileset_name=resolved_name,
        cesium_url_template=url_template,
        published=True,
        stage="done",
    )
    return imagery_url, resolved_name, url_template


def unpublish_completed_job(job_id: str) -> None:
    """Remove published tileset for a job.

    If Redis metadata is gone, attempts to unpublish the symlink named job_id.
    """
    from app.services.job_store import CorruptJobDataError
    from app.services.tile_publisher import unpublish_tileset

    settings = get_settings()
    store = _store()
    tileset_name = job_id
    corrupt_metadata = False

    try:
        data = store.get(job_id)
    except CorruptJobDataError:
        corrupt_metadata = True
        data = None

    if data is None and not corrupt_metadata:
        # Redis expired: still try to remove symlink registered under job_id.
        unpublish_tileset(settings.tilesets_dir, job_id)
        return

    if data is not None:
        tileset_name = data.get("tileset_name") or job_id

    unpublish_tileset(settings.tilesets_dir, tileset_name)
    update_fields: dict[str, object] = {
        "published": False,
        "imagery_url": None,
        "tileset_name": None,
        "cesium_url_template": None,
    }
    if corrupt_metadata:
        store.overwrite(job_id, status=JobStatus.COMPLETED.value, **update_fields)
    else:
        store.update(job_id, **update_fields)


def create_job_from_upload(
    uploaded_path: Path,
    request: ImageryJobCreate,
) -> str:
    """Persist upload and enqueue processing job."""
    settings = get_settings()
    store = _store()
    job_id = str(uuid4())

    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_dest = job_dir / "input.tif"
    shutil.copy2(uploaded_path, input_dest)

    request_with_path = request.model_copy(update={"input_path": str(input_dest)})
    store.create(
        job_id,
        {
            "input_path": str(input_dest),
            "output_dir": str(job_dir / "tiles"),
            "request": request_with_path.model_dump(),
        },
    )
    process_imagery_job.delay(job_id, request_with_path.model_dump())
    return job_id


def create_job_from_path(request: ImageryJobCreate) -> str:
    """Enqueue processing job for an existing workspace path."""
    settings = get_settings()
    store = _store()
    job_id = str(uuid4())

    if not request.input_path:
        raise ValueError("input_path is required")

    input_path = Path(request.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    store.create(
        job_id,
        {
            "input_path": str(input_path),
            "output_dir": str(settings.jobs_dir / job_id / "tiles"),
            "request": request.model_dump(),
        },
    )
    process_imagery_job.delay(job_id, request.model_dump())
    return job_id
