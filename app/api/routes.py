"""REST API 路由。"""

import asyncio
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
    JobStatus,
    PreprocessOptions,
    PublishOptions,
    DiskPublishRequest,
    TilesetInfo,
    TilesetListResponse,
    TilingOptions,
    WorkspaceEntryInfo,
    WorkspaceListResponse,
)
from app.services.job_detail import job_detail_from_store
from app.services.job_store import CorruptJobDataError, JobStore
from app.services.tile_json import crs_label_for_profile, scheme_label
from app.services.tile_publisher import (
    PublishError,
    get_tileset_display_meta,
    list_published_tilesets,
    publish_from_disk,
    unpublish_tileset,
)
from app.services.workspace_browser import WorkspacePathError, list_workspace
from app.worker.tasks import (
    create_job_from_path,
    create_job_from_upload,
    publish_completed_job,
    unpublish_completed_job,
)

router = APIRouter(prefix="/api/v1/imagery", tags=["影像服务"])


def _store() -> JobStore:
    return JobStore(get_settings())


def _job_detail_from_store(data: dict) -> ImageryJobDetail:
    return job_detail_from_store(data)


class ManualPublishRequest(BaseModel):
    tileset_name: str | None = Field(
        default=None,
        description="覆盖瓦片集名称；省略则使用 job_id",
    )


def _tileset_info_from_name(
    name: str,
    settings,
    *,
    tiles_dir: Path | None = None,
) -> TilesetInfo:
    meta = get_tileset_display_meta(settings.tilesets_dir, name, tiles_dir=tiles_dir)
    scheme = meta["scheme"]
    profile = meta["profile"]
    crs = meta["crs"] or crs_label_for_profile(profile)
    return TilesetInfo(
        name=name,
        imagery_url=settings.imagery_url_for(name),
        url_template=meta["url_template"],
        scheme=scheme,
        scheme_label=scheme_label(scheme),
        min_zoom=meta["min_zoom"],
        max_zoom=meta["max_zoom"],
        profile=profile,
        crs=crs,
        bounds=meta["bounds"],
    )


@router.post("/jobs", response_model=ImageryJobResponse, summary="提交切片任务")
async def create_job(request: ImageryJobCreate) -> ImageryJobResponse:
    """为工作区内已有 GeoTIFF 文件提交影像切片任务。"""
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


@router.post("/jobs/upload", response_model=ImageryJobResponse, summary="上传并提交切片任务")
async def create_job_with_upload(
    file: UploadFile = File(..., description="正射影像 GeoTIFF 文件"),
    preprocess_json: str | None = Form(default=None, description="预处理选项 JSON（PreprocessOptions）"),
    tiling_options_json: str | None = Form(default=None, description="切片选项 JSON（TilingOptions）"),
    publish_json: str | None = Form(default=None, description="发布选项 JSON（PublishOptions）"),
) -> ImageryJobResponse:
    """上传 GeoTIFF 正射影像并提交切片任务。"""
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


@router.get("/jobs/{job_id}", response_model=ImageryJobDetail, summary="查询任务状态")
async def get_job(job_id: str) -> ImageryJobDetail:
    """查询任务状态、进度与结果路径。"""
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


@router.post("/jobs/{job_id}/publish", response_model=ImageryJobDetail, summary="发布任务瓦片")
async def publish_job(
    job_id: str,
    body: ManualPublishRequest | None = Body(default=None),
) -> ImageryJobDetail:
    """将已完成任务的瓦片发布到 imagery-server（nginx）。

    若 Redis 任务元数据已过期，则从磁盘 jobs/{job_id}/tiles/ 发布。
    """
    tileset_name = body.tileset_name if body is not None else None
    try:
        imagery_url, resolved_name, url_template = publish_completed_job(
            job_id, tileset_name=tileset_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PublishError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 500
        raise HTTPException(status_code=status, detail=detail) from exc

    try:
        data = _store().get(job_id)
    except CorruptJobDataError as exc:
        raise HTTPException(
            status_code=500,
            detail="Job metadata is corrupted in the store",
        ) from exc

    if data is not None:
        return _job_detail_from_store(data)

    settings = get_settings()
    return ImageryJobDetail(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        stage="done",
        output_dir=str(settings.jobs_dir / job_id / "tiles"),
        imagery_url=imagery_url,
        tileset_name=resolved_name,
        cesium_url_template=url_template,
        published=True,
    )


@router.delete("/jobs/{job_id}/publish", response_model=ImageryJobDetail, summary="取消发布任务瓦片")
async def unpublish_job(job_id: str) -> ImageryJobDetail:
    """移除任务已发布的瓦片集注册。

    若 Redis 元数据已不存在，则删除以 job_id 命名的符号链接（若存在）。
    """
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

    if data is not None:
        return _job_detail_from_store(data)

    return ImageryJobDetail(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        published=False,
    )


@router.get("/workspace", response_model=WorkspaceListResponse, summary="浏览工作区")
async def list_workspace_entries(
    path: str = Query(default="", description="相对于工作区根目录的目录路径"),
) -> WorkspaceListResponse:
    """列出工作区内的目录与可选 GeoTIFF 文件（大目录在线程池中扫描，避免阻塞事件循环）。"""
    settings = get_settings()
    try:
        listing = await asyncio.to_thread(list_workspace, settings.workspace_dir, path)
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


@router.post("/tilesets/publish", response_model=TilesetInfo, summary="从磁盘发布瓦片集")
async def publish_tileset_from_disk(body: DiskPublishRequest) -> TilesetInfo:
    """从磁盘发布瓦片，无需 Redis 任务元数据。

    提供 ``job_id``（使用 ``jobs/{job_id}/tiles/``）或 ``tiles_dir`` 之一。
    元数据优先从已有 ``tile.json`` 推断。
    """
    settings = get_settings()
    try:
        imagery_url, name, _url_template, _tiles_dir = publish_from_disk(
            jobs_dir=settings.jobs_dir,
            workspace_dir=settings.workspace_dir,
            tilesets_dir=settings.tilesets_dir,
            public_url=settings.imagery_server_public_url,
            base_path=settings.imagery_base_path,
            job_id=body.job_id,
            tiles_dir=body.tiles_dir,
            tileset_name=body.tileset_name,
            profile=body.profile,
            tile_format=body.tile_format,
            tile_scheme=body.tile_scheme,
            bounds_wgs84=body.bounds_wgs84,
            gdal_cachemax=settings.gdal_cachemax,
        )
    except PublishError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    _ = imagery_url
    return _tileset_info_from_name(name, settings, tiles_dir=_tiles_dir)


@router.delete("/tilesets/{tileset_name}", response_model=TilesetInfo, summary="取消发布瓦片集")
async def unpublish_tileset_by_name(tileset_name: str) -> TilesetInfo:
    """按名称取消发布瓦片集，无需 Redis 任务元数据。"""
    settings = get_settings()
    info = _tileset_info_from_name(tileset_name, settings)
    try:
        unpublish_tileset(settings.tilesets_dir, tileset_name)
    except PublishError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return info


@router.get("/tilesets", response_model=TilesetListResponse, summary="列出已发布瓦片集")
async def list_tilesets() -> TilesetListResponse:
    """列出已在 imagery-server 注册的瓦片集。

    展示元数据优先读发布旁路的 ``.{name}.layer-meta.json`` / 内存缓存，
    避免每次跟随 symlink 进入大型 tiles 目录。
    """
    settings = get_settings()

    def _load() -> list[TilesetInfo]:
        names = list_published_tilesets(settings.tilesets_dir)
        return [_tileset_info_from_name(name, settings) for name in names]

    tilesets = await asyncio.to_thread(_load)
    return TilesetListResponse(tilesets=tilesets)
