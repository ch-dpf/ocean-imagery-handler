"""Celery tasks for imagery processing pipeline."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import Settings, get_settings
from app.schemas import ImageryJobCreate, JobProgress, JobStatus, TilingOptions
from app.services.imagery_pipeline import (
    PipelineEvent,
    publish_pipeline_tileset,
    resolve_tiling_options,
    run_imagery_pipeline,
)
from app.services.job_progress import (
    ThrottledProgressWriter,
    progress_to_store_fields,
)
from app.services.job_store import JobStore
from app.services.preprocessor import (
    PreprocessError,
    parse_wgs84_bounds,
)
from app.services.raster.errors import RasterError
from app.services.tile_json import TileJsonError, _bounds_valid_wgs84
from app.services.tile_publisher import PublishError
from app.services.tiler_runner import TilerError
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _store() -> JobStore:
    return JobStore(get_settings())


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_tiling_options(tiling: TilingOptions, settings: Settings) -> TilingOptions:
    """Compatibility wrapper for callers of the former task-local helper."""
    return resolve_tiling_options(tiling, settings)


class _JobProgressReporter:
    """Persist pipeline events for the Celery/API execution path."""

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self._store = _store()
        self._writer = ThrottledProgressWriter(self._persist)
        self._state: tuple[JobStatus, str] | None = None
        self.last_progress: JobProgress | None = None

    def _persist(self, progress: JobProgress) -> None:
        self._store.update(self._job_id, **progress_to_store_fields(progress))

    def handle(self, event: PipelineEvent) -> None:
        self.last_progress = event.progress
        state = (event.status, event.stage)
        if state != self._state or event.fields:
            event_fields = {
                key: value
                for key, value in event.fields.items()
                if key not in {"job_id", "status", "stage", "progress"}
            }
            self._store.update(
                self._job_id,
                status=event.status.value,
                stage=event.stage,
                **event_fields,
                **progress_to_store_fields(event.progress),
            )
            self._state = state
            return
        self._writer.emit(event.progress)


@celery_app.task(bind=True, name="imagery.process_job")
def process_imagery_job(self, job_id: str, request_data: dict) -> dict:
    settings = get_settings()
    store = _store()
    request = ImageryJobCreate.model_validate(request_data)
    reporter = _JobProgressReporter(job_id)

    try:
        result = run_imagery_pipeline(
            job_id,
            request,
            settings=settings,
            on_event=reporter.handle,
        )
        result_fields = {key: value for key, value in result.items() if key != "job_id"}
        store.update(
            job_id,
            **result_fields,
            stage="done",
            error=None,
            completed_at=_utc_now_iso(),
        )
        return result

    except (
        PreprocessError,
        RasterError,
        TilerError,
        PublishError,
        TileJsonError,
        OSError,
        ValueError,
    ) as exc:
        logger.exception("Job %s failed", job_id)
        failed_fields: dict = {
            "status": JobStatus.FAILED.value,
            "stage": "failed",
            "error": str(exc),
            "completed_at": _utc_now_iso(),
        }
        if reporter.last_progress is not None:
            failed_progress = reporter.last_progress.model_copy(
                update={"message": str(exc), "phase": "failed"}
            )
            failed_fields.update(progress_to_store_fields(failed_progress))
        store.update(job_id, **failed_fields)
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

    store.update(job_id, status=JobStatus.PUBLISHING.value, stage="register_tileset")
    imagery_url, resolved_name, url_template = publish_pipeline_tileset(
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
