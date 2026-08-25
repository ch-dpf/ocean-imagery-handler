"""Celery tasks for imagery processing pipeline."""

import logging
import shutil
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.schemas import ImageryJobCreate, JobStatus
from app.services.imagery_metadata import ImageryMetadataError, _bounds_valid_wgs84
from app.services.job_store import JobStore
from app.services.preprocessor import PreprocessError, parse_wgs84_bounds, preprocess_imagery
from app.services.tile_publisher import PublishError, publish_tileset
from app.services.tiler_runner import TilerError, run_gdal2tiles
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _store() -> JobStore:
    return JobStore(get_settings())


def _should_auto_publish(request: ImageryJobCreate, settings) -> bool:
    if request.publish.auto_publish is not None:
        return request.publish.auto_publish
    return settings.auto_publish


def _publish_job_tileset(
    job_id: str,
    output_dir: Path,
    request: ImageryJobCreate,
    settings,
    bounds_wgs84: list[float],
) -> tuple[str, str, str]:
    store = _store()
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

    try:
        store.update(job_id, status=JobStatus.RUNNING.value, stage="initializing")

        if not request.input_path:
            raise ValueError("input_path is required for background processing")

        input_path = Path(request.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        store.update(job_id, status=JobStatus.PREPROCESSING.value, stage="gdal_preprocess")
        preprocessed = preprocess_imagery(
            input_path=input_path,
            work_dir=preprocess_dir,
            options=request.preprocess,
            gdal_cachemax=settings.gdal_cachemax,
        )

        bounds_wgs84 = parse_wgs84_bounds(preprocessed, env={"GDAL_CACHEMAX": str(settings.gdal_cachemax)})
        store.update(job_id, bounds_wgs84=bounds_wgs84)

        store.update(job_id, status=JobStatus.TILING.value, stage="gdal2tiles")
        run_gdal2tiles(
            input_path=preprocessed,
            output_dir=output_dir,
            options=request.tiling_options,
            gdal_cachemax=settings.gdal_cachemax,
        )

        result: dict[str, str | bool | list[float]] = {
            "job_id": job_id,
            "status": JobStatus.COMPLETED.value,
            "output_dir": str(output_dir),
            "bounds_wgs84": bounds_wgs84,
            "published": False,
        }

        if _should_auto_publish(request, settings):
            imagery_url, tileset_name, url_template = _publish_job_tileset(
                job_id, output_dir, request, settings, bounds_wgs84
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
            )
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
            )

        return result

    except (
        PreprocessError,
        TilerError,
        PublishError,
        ImageryMetadataError,
        OSError,
        ValueError,
    ) as exc:
        logger.exception("Job %s failed", job_id)
        store.update(
            job_id,
            status=JobStatus.FAILED.value,
            stage="failed",
            error=str(exc),
        )
        raise


def publish_completed_job(job_id: str, tileset_name: str | None = None) -> tuple[str, str, str]:
    """Publish tiles for a completed job (manual API)."""
    settings = get_settings()
    store = _store()
    data = store.get(job_id)
    if data is None:
        raise ValueError(f"Job not found: {job_id}")

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
            # Force imagery.json regeneration with corrected bounds.
            imagery_json = Path(output_dir) / "imagery.json"
            imagery_json.unlink(missing_ok=True)

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
    """Remove published tileset for a job."""
    from app.services.tile_publisher import unpublish_tileset

    settings = get_settings()
    store = _store()
    data = store.get(job_id)
    if data is None:
        raise ValueError(f"Job not found: {job_id}")

    tileset_name = data.get("tileset_name") or job_id
    unpublish_tileset(settings.tilesets_dir, tileset_name)
    store.update(
        job_id,
        published=False,
        imagery_url=None,
        tileset_name=None,
        cesium_url_template=None,
    )


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
