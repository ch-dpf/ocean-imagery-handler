"""REST API routes."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.schemas import (
    ImageryJobCreate,
    ImageryJobDetail,
    ImageryJobResponse,
    JobProgress,
    JobStatus,
    PreprocessOptions,
    PublishOptions,
    TilesetInfo,
    TilesetListResponse,
    TilingOptions,
    WorkspaceEntryInfo,
    WorkspaceListResponse,
)
from app.services.job_store import CorruptJobDataError, JobStore
from app.services.tile_json import TILE_JSON, crs_label_for_profile, scheme_label
from app.services.tile_publisher import PublishError, list_published_tilesets
from app.services.workspace_browser import WorkspacePathError, list_workspace
from app.worker.tasks import (
    create_job_from_path,
    create_job_from_upload,
    publish_completed_job,
    unpublish_completed_job,
)

router = APIRouter(prefix="/api/v1/imagery", tags=["imagery"])

_JOB_DETAIL_FIELDS = {
    "job_id",
    "status",
    "stage",
    "progress",
    "input_path",
    "output_dir",
    "imagery_url",
    "tileset_name",
    "cesium_url_template",
    "published",
    "bounds_wgs84",
    "error",
}


def _store() -> JobStore:
    return JobStore(get_settings())


def _progress_from_store(data: dict) -> JobProgress | None:
    raw = data.get("progress")
    if not raw:
        return None
    return JobProgress.model_validate(raw)


def _job_detail_from_store(data: dict) -> ImageryJobDetail:
    return ImageryJobDetail(
        job_id=data["job_id"],
        status=JobStatus(data["status"]),
        progress=_progress_from_store(data),
        stage=data.get("stage"),
        input_path=data.get("input_path"),
        output_dir=data.get("output_dir"),
        imagery_url=data.get("imagery_url"),
        tileset_name=data.get("tileset_name"),
        cesium_url_template=data.get("cesium_url_template"),
        published=bool(data.get("published")),
        error=data.get("error"),
        metadata={
            key: value
            for key, value in data.items()
            if key not in _JOB_DETAIL_FIELDS | {"request"}
        },
    )


class ManualPublishRequest(BaseModel):
    tileset_name: str | None = Field(
        default=None,
        description="Override tileset name; omit to use job_id",
    )


def _tileset_info_from_name(name: str, settings) -> TilesetInfo:
    url_template = None
    scheme = None
    min_zoom = None
    max_zoom = None
    profile = None
    crs = None
    bounds = None

    link_path = settings.tilesets_dir / name
    metadata_path = link_path / TILE_JSON
    if metadata_path.is_file():
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            tiles = meta.get("tiles") or []
            url_template = tiles[0] if tiles else None
            scheme = meta.get("scheme")
            min_zoom = meta.get("minzoom")
            max_zoom = meta.get("maxzoom")
            profile = meta.get("profile")
            crs = crs_label_for_profile(profile)
            raw_bounds = meta.get("bounds")
            if isinstance(raw_bounds, list) and len(raw_bounds) == 4:
                bounds = [float(v) for v in raw_bounds]
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    if crs is None:
        crs = crs_label_for_profile(None)

    return TilesetInfo(
        name=name,
        imagery_url=settings.imagery_url_for(name),
        url_template=url_template,
        scheme=scheme,
        scheme_label=scheme_label(scheme),
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        profile=profile,
        crs=crs,
        bounds=bounds,
    )


@router.post("/jobs", response_model=ImageryJobResponse)
async def create_job(request: ImageryJobCreate) -> ImageryJobResponse:
    """Submit a tiling job for an existing file in the workspace."""
    try:
        job_id = create_job_from_path(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ImageryJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress_url=f"/api/v1/imagery/jobs/{job_id}",
        message="Job queued",
    )


@router.post("/jobs/upload", response_model=ImageryJobResponse)
async def create_job_with_upload(
    file: UploadFile = File(...),
    preprocess_json: str | None = Form(default=None),
    tiling_options_json: str | None = Form(default=None),
    publish_json: str | None = Form(default=None),
) -> ImageryJobResponse:
    """Upload a GeoTIFF orthophoto and submit a tiling job."""
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".tif", ".tiff", ".img"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    request = ImageryJobCreate()
    if preprocess_json:
        request.preprocess = PreprocessOptions.model_validate(json.loads(preprocess_json))
    if tiling_options_json:
        request.tiling_options = TilingOptions.model_validate(json.loads(tiling_options_json))
    if publish_json:
        request.publish = PublishOptions.model_validate(json.loads(publish_json))

    temp_path = settings.uploads_dir / f"{uuid4()}{suffix}"
    with temp_path.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    try:
        job_id = create_job_from_upload(temp_path, request)
    finally:
        temp_path.unlink(missing_ok=True)

    return ImageryJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress_url=f"/api/v1/imagery/jobs/{job_id}",
        message="Upload received, job queued",
    )


@router.get("/jobs/{job_id}", response_model=ImageryJobDetail)
async def get_job(job_id: str) -> ImageryJobDetail:
    """Get job status and result paths."""
    try:
        data = _store().get(job_id)
    except CorruptJobDataError as exc:
        raise HTTPException(
            status_code=500,
            detail="Job metadata is corrupted in the store",
        ) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return _job_detail_from_store(data)


@router.post("/jobs/{job_id}/publish", response_model=ImageryJobDetail)
async def publish_job(
    job_id: str,
    body: ManualPublishRequest | None = Body(default=None),
) -> ImageryJobDetail:
    """Publish a completed job's tiles via imagery-server (nginx)."""
    tileset_name = body.tileset_name if body is not None else None
    try:
        publish_completed_job(job_id, tileset_name=tileset_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        data = _store().get(job_id)
    except CorruptJobDataError as exc:
        raise HTTPException(
            status_code=500,
            detail="Job metadata is corrupted in the store",
        ) from exc
    assert data is not None
    return _job_detail_from_store(data)


@router.delete("/jobs/{job_id}/publish", response_model=ImageryJobDetail)
async def unpublish_job(job_id: str) -> ImageryJobDetail:
    """Remove a job's published tileset registration."""
    try:
        unpublish_completed_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        data = _store().get(job_id)
    except CorruptJobDataError as exc:
        raise HTTPException(
            status_code=500,
            detail="Job metadata is corrupted in the store",
        ) from exc
    assert data is not None
    return _job_detail_from_store(data)


@router.get("/workspace", response_model=WorkspaceListResponse)
async def list_workspace_entries(
    path: str = Query(default="", description="Directory path relative to workspace root"),
) -> WorkspaceListResponse:
    """List directories and selectable GeoTIFF files in the workspace."""
    settings = get_settings()
    try:
        listing = list_workspace(settings.workspace_dir, path)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return WorkspaceListResponse(
        relative_path=listing.relative_path,
        absolute_path=listing.absolute_path,
        parent_relative_path=listing.parent_relative_path,
        entries=[
            WorkspaceEntryInfo(
                name=entry.name,
                relative_path=entry.relative_path,
                absolute_path=entry.absolute_path,
                entry_type=entry.entry_type,
                size_bytes=entry.size_bytes,
                selectable=entry.selectable,
            )
            for entry in listing.entries
        ],
    )


@router.get("/tilesets", response_model=TilesetListResponse)
async def list_tilesets() -> TilesetListResponse:
    """List tilesets registered for imagery-server."""
    settings = get_settings()
    names = list_published_tilesets(settings.tilesets_dir)
    tilesets: list[TilesetInfo] = []

    for name in names:
        tilesets.append(_tileset_info_from_name(name, settings))

    return TilesetListResponse(tilesets=tilesets)
