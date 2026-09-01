"""Imagery preprocessing pipeline (Python raster engine, no GDAL CLI)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.raster.errors import RasterError
from app.services.raster.info import raster_info_json, raster_info_text, wgs84_bounds
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import reproject_geotiff

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str | None], None]


class PreprocessError(RuntimeError):
    pass


def _cache_bytes(gdal_cachemax: int | None) -> int:
    megabytes = max(int(gdal_cachemax or 64), 1)
    return megabytes * 1024 * 1024


def gdal_info(dataset: Path, env: dict[str, str] | None = None) -> str:
    """Return human-readable raster metadata (kept name for compatibility)."""
    del env
    try:
        return raster_info_text(dataset)
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc


def _raster_info_json(dataset: Path, env: dict[str, str] | None = None) -> dict:
    del env
    try:
        return raster_info_json(dataset)
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc


def _bounds_from_wgs84_extent(data: dict) -> list[float] | None:
    extent = data.get("wgs84Extent")
    if not extent:
        return None
    coordinates = extent.get("coordinates")
    if not coordinates:
        return None
    ring = coordinates[0]
    if not ring:
        return None
    lons = [float(point[0]) for point in ring]
    lats = [float(point[1]) for point in ring]
    return [min(lons), min(lats), max(lons), max(lats)]


def _bounds_valid_wgs84(bounds: list[float] | list) -> bool:
    try:
        west, south, east, north = [float(value) for value in bounds]
    except (TypeError, ValueError):
        return False
    return (
        -180.0 <= west <= 180.0
        and -180.0 <= east <= 180.0
        and -90.0 <= south <= 90.0
        and -90.0 <= north <= 90.0
        and west < east
        and south < north
    )


def parse_wgs84_bounds(dataset: Path, env: dict[str, str] | None = None) -> list[float]:
    """Return [west, south, east, north] in WGS84."""
    del env
    try:
        data = raster_info_json(dataset)
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc

    bounds = _bounds_from_wgs84_extent(data)
    if bounds is not None and _bounds_valid_wgs84(bounds):
        return bounds

    stored = data.get("wgs84Bounds")
    if isinstance(stored, list) and _bounds_valid_wgs84(stored):
        return [float(v) for v in stored]

    try:
        bounds = wgs84_bounds(dataset)
    except RasterError:
        return [-180.0, -90.0, 180.0, 90.0]
    if _bounds_valid_wgs84(bounds):
        return bounds
    return [-180.0, -90.0, 180.0, 90.0]


def preprocess_imagery(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: ProgressFn | None = None,
) -> Path:
    """Reproject, optionally build overviews, and return a tiling-ready GeoTIFF."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_bytes = _cache_bytes(gdal_cachemax)
    warped = work_dir / "warped.tif"
    final = work_dir / "preprocessed.tif"

    compress = options.compress.upper()
    needs_alpha = options.add_alpha or options.white_as_transparent
    if needs_alpha and compress == "JPEG":
        logger.warning("compress=JPEG is incompatible with alpha transparency; using DEFLATE instead")
        compress = "DEFLATE"

    if options.white_as_transparent and options.near_white > 0:
        logger.warning(
            "near_white=%s is ignored; only exact RGB(255,255,255) is treated as transparent",
            options.near_white,
        )

    warp_weight = 0.85 if options.build_overviews else 1.0
    addo_weight = 0.15

    def _emit_reproject(sub_percent: float, message: str | None) -> None:
        if on_subprogress is None:
            return
        scaled = sub_percent * warp_weight
        on_subprogress(scaled, message or "reproject")

    try:
        reproject_geotiff(
            input_path,
            warped,
            dst_crs=options.target_crs,
            compress=compress,
            block_size=options.block_size,
            jpeg_quality=options.jpeg_quality,
            add_alpha=options.add_alpha,
            white_as_transparent=options.white_as_transparent,
            cache_bytes=cache_bytes,
            resampling="bilinear",
            on_progress=_emit_reproject if on_subprogress is not None else None,
        )
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc

    if options.build_overviews:

        def _emit_overview(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            scaled = warp_weight * 100.0 + sub_percent * addo_weight
            on_subprogress(min(scaled, 100.0), message or "overview add")

        try:
            add_overviews(
                warped,
                block_size=options.block_size,
                compress=compress if compress != "JPEG" else "DEFLATE",
                jpeg_quality=options.jpeg_quality,
                cache_bytes=cache_bytes,
                on_progress=_emit_overview if on_subprogress is not None else None,
            )
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc

    if final.exists() or final.is_symlink():
        final.unlink()
    warped.replace(final)
    ovr_src = Path(str(warped) + ".ovr")
    ovr_dst = Path(str(final) + ".ovr")
    if ovr_src.is_file():
        if ovr_dst.exists():
            ovr_dst.unlink()
        ovr_src.replace(ovr_dst)

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")
    return final
