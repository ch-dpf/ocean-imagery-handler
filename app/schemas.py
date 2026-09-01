"""Pydantic 请求/响应模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PREPROCESSING = "preprocessing"
    TILING = "tiling"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"


class ResamplingMethod(str, Enum):
    NEAREST = "near"
    BILINEAR = "bilinear"
    CUBIC = "cubic"
    CUBICSPLINE = "cubicspline"
    LANCZOS = "lanczos"
    ANTIALIAS = "antialias"
    AVERAGE = "average"
    MODE = "mode"


class TileProfile(str, Enum):
    MERCATOR = "mercator"
    GEODETIC = "geodetic"
    RASTER = "raster"


class TileFormat(str, Enum):
    PNG = "PNG"
    JPEG = "JPEG"
    WEBP = "WEBP"


class TileScheme(str, Enum):
    XYZ = "xyz"
    TMS = "tms"


class PreprocessOptions(BaseModel):
    target_crs: str = Field(
        default="EPSG:3857",
        description="重投影目标坐标系（Cesium Web Mercator 推荐 EPSG:3857）",
    )
    build_overviews: bool = Field(default=True, description="是否构建 GeoTIFF 低分辨率概览图（overview）")
    block_size: int = Field(
        default=256,
        ge=16,
        description="GeoTIFF 分块大小 BLOCKXSIZE/BLOCKYSIZE，须为 16 的倍数",
    )
    compress: str = Field(
        default="DEFLATE",
        description="GeoTIFF 压缩方式：DEFLATE、LZW 或 JPEG（JPEG 无法保留 alpha）",
    )
    jpeg_quality: int = Field(default=85, ge=1, le=100, description="compress=JPEG 时的 JPEG 质量（1–100）")
    add_alpha: bool = Field(
        default=True,
        description="是否添加 alpha 通道，使影像足迹外区域在瓦片中透明",
    )
    white_as_transparent: bool = Field(
        default=False,
        description="重投影时将纯白 RGB(255,255,255) 填充视为透明",
    )
    near_white: int = Field(
        default=0,
        ge=0,
        le=255,
        description="保留字段，当前未使用（仅精确白色 255,255,255 会被掩膜）",
    )

    @field_validator("block_size")
    @classmethod
    def block_size_multiple_of_16(cls, value: int) -> int:
        if value % 16 != 0:
            raise ValueError("block_size 必须为 16 的倍数（GeoTIFF TileWidth 要求）")
        return value


class TilingOptions(BaseModel):
    profile: TileProfile = Field(
        default=TileProfile.MERCATOR,
        description="切片方案：mercator（WebMercatorQuad）、geodetic（WorldCRS84Quad）或 raster",
    )
    tile_format: TileFormat = Field(default=TileFormat.PNG, description="输出瓦片图像格式")
    tile_size: int = Field(default=256, ge=1, description="瓦片像素尺寸")
    start_zoom: int | None = Field(
        default=None,
        ge=0,
        description="最大缩放级别（最精细）；省略则按源分辨率自动推算",
    )
    end_zoom: int = Field(default=0, ge=0, description="最小缩放级别")
    resampling_method: ResamplingMethod = Field(
        default=ResamplingMethod.BILINEAR,
        description="重采样方法",
    )
    thread_count: int | None = Field(
        default=None,
        ge=1,
        description="瓦片生成并行任务数；默认取 TILING_THREAD_COUNT",
    )
    resume: bool | None = Field(
        default=None,
        description="是否断点续切；默认取 TILING_RESUME",
    )
    verbose: bool = Field(default=False, description="是否输出详细日志")
    kml: bool = Field(default=False, description="是否生成 KML 概览")
    tile_scheme: TileScheme = Field(
        default=TileScheme.XYZ,
        description="瓦片坐标方案：xyz（Slippy/OSM，默认）或 tms",
    )


class PublishOptions(BaseModel):
    auto_publish: bool | None = Field(
        default=None,
        description="任务完成后是否自动发布瓦片集；默认取 AUTO_PUBLISH 配置",
    )
    tileset_name: str | None = Field(
        default=None,
        description="发布后的瓦片集名称；默认为 job_id",
    )


class ImageryJobCreate(BaseModel):
    input_path: str | None = Field(
        default=None,
        description="工作区内输入 TIF 的绝对路径（与上传接口互斥）",
    )
    preprocess: PreprocessOptions = Field(default_factory=PreprocessOptions, description="预处理选项")
    tiling_options: TilingOptions = Field(default_factory=TilingOptions, description="切片选项")
    publish: PublishOptions = Field(default_factory=PublishOptions, description="发布选项")

    @field_validator("input_path")
    @classmethod
    def strip_input_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        return stripped or None


class ImageryJobResponse(BaseModel):
    job_id: str = Field(description="任务 ID")
    status: JobStatus = Field(description="任务状态")
    progress_url: str = Field(description="进度查询 URL")
    message: str | None = Field(default=None, description="提示信息")


class JobProgress(BaseModel):
    percent: float = Field(ge=0, le=100, description="整体完成进度（0–100）")
    phase: str | None = Field(default=None, description="当前流水线阶段标识")
    message: str | None = Field(default=None, description="可读的进度详情")
    current_zoom: int | None = Field(default=None, description="正在生成的缩放级别")
    min_zoom: int | None = Field(default=None, description="输出最小缩放级别")
    max_zoom: int | None = Field(default=None, description="输出最大缩放级别")
    weight_source: str | None = Field(
        default="bytes",
        description="进度计量单位：已写入/计划的未压缩栅格字节数",
    )
    bytes_done: int | None = Field(default=None, ge=0, description="已完成的未压缩栅格字节数")
    bytes_planned: int | None = Field(default=None, ge=0, description="任务计划中的未压缩栅格字节数")
    calibration_samples: int | None = Field(
        default=None,
        description="保留字段，用于 API 兼容",
    )


class ImageryJobDetail(BaseModel):
    job_id: str = Field(description="任务 ID")
    status: JobStatus = Field(description="任务状态")
    progress: JobProgress | None = Field(default=None, description="进度详情")
    stage: str | None = Field(default=None, description="当前阶段")
    created_at: str | None = Field(
        default=None,
        description="任务创建时间（UTC ISO-8601）",
    )
    completed_at: str | None = Field(
        default=None,
        description="任务结束时间（UTC ISO-8601，完成或失败）",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        ge=0,
        description="从 created_at 到 completed_at 的耗时（秒）；运行中则为至今耗时",
    )
    input_path: str | None = Field(default=None, description="输入文件路径")
    output_dir: str | None = Field(default=None, description="输出目录")
    imagery_url: str | None = Field(default=None, description="影像服务访问 URL")
    tileset_name: str | None = Field(default=None, description="已发布的瓦片集名称")
    published: bool = Field(default=False, description="是否已发布")
    cesium_url_template: str | None = Field(default=None, description="Cesium 瓦片 URL 模板")
    error: str | None = Field(default=None, description="失败时的错误信息")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class TilesetInfo(BaseModel):
    name: str = Field(description="瓦片集名称")
    imagery_url: str = Field(description="影像服务根 URL")
    url_template: str | None = Field(default=None, description="瓦片 URL 模板")
    scheme: str | None = Field(default=None, description="瓦片索引方案：xyz 或 tms")
    scheme_label: str | None = Field(default=None, description="方案的可读标签")
    min_zoom: int | None = Field(default=None, description="最小缩放级别")
    max_zoom: int | None = Field(default=None, description="最大缩放级别")
    profile: str | None = Field(default=None, description="tile.json 中的切片 profile")
    crs: str | None = Field(default=None, description="瓦片坐标系标签")
    bounds: list[float] | None = Field(
        default=None,
        description="WGS84 范围 [west, south, east, north]",
    )


class TilesetListResponse(BaseModel):
    tilesets: list[TilesetInfo] = Field(description="已注册瓦片集列表")


class DiskPublishRequest(BaseModel):
    """从磁盘发布瓦片，无需 Redis 任务元数据。"""

    job_id: str | None = Field(
        default=None,
        description="发布 jobs/{job_id}/tiles/（与 tiles_dir 互斥）",
    )
    tiles_dir: str | None = Field(
        default=None,
        description="瓦片目录的绝对路径或工作区相对路径（与 job_id 互斥）",
    )
    tileset_name: str | None = Field(
        default=None,
        description="发布后的瓦片集名称；默认为 job_id",
    )
    profile: TileProfile | None = Field(
        default=None,
        description="覆盖切片 profile；默认取 tile.json 或 mercator",
    )
    tile_format: TileFormat | None = Field(
        default=None,
        description="覆盖瓦片格式；默认取 tile.json 或 PNG",
    )
    tile_scheme: TileScheme | None = Field(
        default=None,
        description="覆盖瓦片坐标方案；默认取 tile.json 或 xyz",
    )
    bounds_wgs84: list[float] | None = Field(
        default=None,
        description="覆盖 WGS84 范围 [west, south, east, north]",
    )


class WorkspaceEntryInfo(BaseModel):
    name: str = Field(description="条目名称")
    relative_path: str = Field(description="相对于工作区根目录的路径")
    absolute_path: str = Field(description="绝对路径")
    entry_type: str = Field(description="条目类型：directory 或 file")
    size_bytes: int | None = Field(default=None, description="文件大小（字节）")
    selectable: bool = Field(description="是否可作为切片输入选择")


class WorkspaceListResponse(BaseModel):
    relative_path: str = Field(description="当前目录相对于工作区根目录的路径")
    absolute_path: str = Field(description="当前目录绝对路径")
    parent_relative_path: str | None = Field(default=None, description="父目录相对路径")
    entries: list[WorkspaceEntryInfo] = Field(description="目录条目列表")
