"""Pydantic request/response models."""

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
        description="Target CRS for reprojection (EPSG:3857 recommended for Cesium Web Mercator)",
    )
    build_overviews: bool = Field(default=True, description="Build reduced-resolution GeoTIFF overviews")
    block_size: int = Field(
        default=256,
        ge=16,
        description="GeoTIFF tile BLOCKXSIZE/BLOCKYSIZE; must be a multiple of 16",
    )
    compress: str = Field(default="DEFLATE", description="GeoTIFF compression: DEFLATE, LZW, or JPEG (JPEG cannot keep alpha)")
    jpeg_quality: int = Field(default=85, ge=1, le=100, description="JPEG quality when compress=JPEG")
    add_alpha: bool = Field(
        default=True,
        description="Add alpha band so areas outside the imagery footprint stay transparent in tiles",
    )
    white_as_transparent: bool = Field(
        default=False,
        description="Treat exact white RGB(255,255,255) fill as transparent during reprojection",
    )
    near_white: int = Field(
        default=0,
        ge=0,
        le=255,
        description="Reserved; currently ignored (only exact white 255,255,255 is masked)",
    )

    @field_validator("block_size")
    @classmethod
    def block_size_multiple_of_16(cls, value: int) -> int:
        if value % 16 != 0:
            raise ValueError("block_size must be a multiple of 16 (GeoTIFF TileWidth requirement)")
        return value


class TilingOptions(BaseModel):
    profile: TileProfile = Field(
        default=TileProfile.MERCATOR,
        description=(
            "Tiling scheme profile: mercator (WebMercatorQuad), "
            "geodetic (WorldCRS84Quad), or raster"
        ),
    )
    tile_format: TileFormat = Field(default=TileFormat.PNG, description="Output tile image format")
    tile_size: int = Field(default=256, ge=1, description="Tile pixel size")
    start_zoom: int | None = Field(
        default=None,
        ge=0,
        description="Maximum zoom level (most detailed); omit to auto-detect from source resolution",
    )
    end_zoom: int = Field(default=0, ge=0, description="Minimum zoom level")
    resampling_method: ResamplingMethod = ResamplingMethod.BILINEAR
    thread_count: int | None = Field(
        default=None,
        ge=1,
        description="Parallel jobs for tile generation; defaults to TILING_THREAD_COUNT",
    )
    resume: bool | None = Field(
        default=None,
        description="Resume interrupted tiling; defaults to TILING_RESUME",
    )
    verbose: bool = False
    kml: bool = Field(default=False, description="Generate KML overview")
    tile_scheme: TileScheme = Field(
        default=TileScheme.XYZ,
        description="Tile coordinate scheme: xyz (Slippy/OSM, default) or tms",
    )


class PublishOptions(BaseModel):
    auto_publish: bool | None = Field(
        default=None,
        description="Publish tileset when job completes; defaults to AUTO_PUBLISH setting",
    )
    tileset_name: str | None = Field(
        default=None,
        description="Published tileset name; defaults to job_id",
    )


class ImageryJobCreate(BaseModel):
    input_path: str | None = Field(
        default=None,
        description="Absolute path to input TIF inside workspace (mutually exclusive with upload)",
    )
    preprocess: PreprocessOptions = Field(default_factory=PreprocessOptions)
    tiling_options: TilingOptions = Field(default_factory=TilingOptions)
    publish: PublishOptions = Field(default_factory=PublishOptions)

    @field_validator("input_path")
    @classmethod
    def strip_input_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        return stripped or None


class ImageryJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress_url: str
    message: str | None = None


class JobProgress(BaseModel):
    percent: float = Field(ge=0, le=100, description="Overall job completion 0-100")
    phase: str | None = Field(default=None, description="Current pipeline stage identifier")
    message: str | None = Field(default=None, description="Human-readable progress detail")
    current_zoom: int | None = Field(default=None, description="Zoom level currently being generated")
    min_zoom: int | None = Field(default=None, description="Minimum output zoom level")
    max_zoom: int | None = Field(default=None, description="Maximum output zoom level")
    weight_source: str | None = Field(
        default=None,
        description="Stage weight source: default (fixed) or historical (calibrated from past jobs)",
    )
    calibration_samples: int | None = Field(
        default=None,
        description="Number of completed jobs used for historical calibration, if applicable",
    )


class ImageryJobDetail(BaseModel):
    job_id: str
    status: JobStatus
    progress: JobProgress | None = None
    stage: str | None = None
    created_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp when the job was created",
    )
    completed_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp when the job finished (completed or failed)",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        ge=0,
        description="Wall-clock seconds from created_at to completed_at (or now if still running)",
    )
    input_path: str | None = None
    output_dir: str | None = None
    imagery_url: str | None = None
    tileset_name: str | None = None
    published: bool = False
    cesium_url_template: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TilesetInfo(BaseModel):
    name: str
    imagery_url: str
    url_template: str | None = None
    scheme: str | None = Field(default=None, description="Tile index scheme: xyz or tms")
    scheme_label: str | None = Field(default=None, description="Human-readable scheme label")
    min_zoom: int | None = None
    max_zoom: int | None = None
    profile: str | None = Field(default=None, description="Tiling profile from tile.json")
    crs: str | None = Field(default=None, description="Tile coordinate system label")
    bounds: list[float] | None = Field(
        default=None,
        description="WGS84 bounds [west, south, east, north]",
    )


class TilesetListResponse(BaseModel):
    tilesets: list[TilesetInfo]


class DiskPublishRequest(BaseModel):
    """Publish tiles from disk without requiring Redis job metadata."""

    job_id: str | None = Field(
        default=None,
        description="Publish jobs/{job_id}/tiles/ (mutually exclusive with tiles_dir)",
    )
    tiles_dir: str | None = Field(
        default=None,
        description="Absolute or workspace-relative tiles directory (mutually exclusive with job_id)",
    )
    tileset_name: str | None = Field(
        default=None,
        description="Published tileset name; defaults to job_id",
    )
    profile: TileProfile | None = Field(
        default=None,
        description="Override tiling profile; defaults to tile.json or mercator",
    )
    tile_format: TileFormat | None = Field(
        default=None,
        description="Override tile format; defaults to tile.json or PNG",
    )
    tile_scheme: TileScheme | None = Field(
        default=None,
        description="Override tile scheme; defaults to tile.json or xyz",
    )
    bounds_wgs84: list[float] | None = Field(
        default=None,
        description="Override WGS84 bounds [west, south, east, north]",
    )


class WorkspaceEntryInfo(BaseModel):
    name: str
    relative_path: str
    absolute_path: str
    entry_type: str
    size_bytes: int | None = None
    selectable: bool


class WorkspaceListResponse(BaseModel):
    relative_path: str
    absolute_path: str
    parent_relative_path: str | None = None
    entries: list[WorkspaceEntryInfo]
