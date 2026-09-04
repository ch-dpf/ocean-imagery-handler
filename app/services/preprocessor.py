"""Imagery preprocessing pipeline (Python raster engine, no GDAL CLI)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.schemas import PreprocessOptions
from app.services.byte_progress import fraction_to_bytes, overview_bytes, raster_bytes
from app.services.raster.crsutil import parse_crs
from app.services.raster.errors import RasterError
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.info import raster_info_json, raster_info_text, wgs84_bounds
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import destination_sample_count, plan_destination_grid, reproject_geotiff

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str | None], None]


class PreprocessError(RuntimeError):
    pass


def _cache_bytes(gdal_cachemax: int | None) -> int:
    megabytes = max(int(gdal_cachemax or 64), 1)
    return megabytes * 1024 * 1024


def gdal_info(
    dataset: Path,
    env: dict[str, str] | None = None,
    *,
    gdal_cachemax: int | None = None,
) -> str:
    """Return human-readable raster metadata (kept name for compatibility)."""
    cachemax = gdal_cachemax
    if cachemax is None and env and env.get("GDAL_CACHEMAX"):
        try:
            cachemax = int(env["GDAL_CACHEMAX"])
        except ValueError:
            cachemax = None
    try:
        return raster_info_text(dataset, cache_bytes=_cache_bytes(cachemax))
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc


def _raster_info_json(
    dataset: Path,
    env: dict[str, str] | None = None,
    *,
    gdal_cachemax: int | None = None,
) -> dict:
    cachemax = gdal_cachemax
    if cachemax is None and env and env.get("GDAL_CACHEMAX"):
        try:
            cachemax = int(env["GDAL_CACHEMAX"])
        except ValueError:
            cachemax = None
    try:
        return raster_info_json(dataset, cache_bytes=_cache_bytes(cachemax))
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
    cachemax: int | None = None
    if env and env.get("GDAL_CACHEMAX"):
        try:
            cachemax = int(env["GDAL_CACHEMAX"])
        except ValueError:
            cachemax = None
    try:
        data = raster_info_json(dataset, cache_bytes=_cache_bytes(cachemax))
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc

    bounds = _bounds_from_wgs84_extent(data)
    if bounds is not None and _bounds_valid_wgs84(bounds):
        return bounds

    stored = data.get("wgs84Bounds")
    if isinstance(stored, list) and _bounds_valid_wgs84(stored):
        return [float(v) for v in stored]

    try:
        bounds = wgs84_bounds(dataset, cache_bytes=_cache_bytes(cachemax))
    except RasterError:
        return [-180.0, -90.0, 180.0, 90.0]
    if _bounds_valid_wgs84(bounds):
        return bounds
    return [-180.0, -90.0, 180.0, 90.0]


def validate_source_imagery(
    dataset: Path,
    *,
    gdal_cachemax: int | None = None,
    target_crs: str | None = None,
) -> dict:
    """Preflight: read size / CRS / WGS84 bounds before expensive preprocess.

    Raises ``PreprocessError`` when the GeoTIFF cannot be opened or lacks usable
    georeferencing. Returns the ``raster_info_json`` payload with normalized
    ``wgs84Bounds``.
    """
    if target_crs is not None:
        try:
            parse_crs(target_crs)
        except RasterError as exc:
            raise PreprocessError(str(exc)) from exc

    try:
        data = raster_info_json(dataset, cache_bytes=_cache_bytes(gdal_cachemax))
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc

    size = data.get("size")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or int(size[0]) <= 0
        or int(size[1]) <= 0
    ):
        raise PreprocessError(f"Invalid raster size: {size}")

    crs = data.get("coordinateSystem") or {}
    if not isinstance(crs, dict) or (not crs.get("wkt") and crs.get("epsg") is None):
        raise PreprocessError("Raster has no recognizable CRS")

    bounds = _bounds_from_wgs84_extent(data)
    if bounds is None or not _bounds_valid_wgs84(bounds):
        stored = data.get("wgs84Bounds")
        if isinstance(stored, list) and _bounds_valid_wgs84(stored):
            bounds = [float(v) for v in stored]
        else:
            raise PreprocessError("Could not derive valid WGS84 bounds from raster")

    data["wgs84Bounds"] = bounds
    logger.info(
        "Validated source %s: size=%sx%s crs=EPSG:%s bounds=%s",
        dataset,
        size[0],
        size[1],
        crs.get("epsg"),
        bounds,
    )
    return data


def _unlink_if_exists(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def preprocess_imagery(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: ProgressFn | None = None,
) -> Path:
    """Reproject, optionally build overviews, and return a tiling-ready GeoTIFF.

    Writes directly to ``preprocessed.tif`` (no intermediate rename/copy). Docker
    Desktop bind mounts on Windows often reject ``os.replace``, and copying the
    full orthophoto would double disk I/O.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_bytes = _cache_bytes(gdal_cachemax)
    final = work_dir / "preprocessed.tif"
    final_ovr = Path(str(final) + ".ovr")
    # Drop leftovers from interrupted runs (including legacy warped.tif names).
    for leftover in (
        final,
        final_ovr,
        work_dir / "warped.tif",
        Path(str(work_dir / "warped.tif") + ".ovr"),
    ):
        _unlink_if_exists(leftover)

    validate_source_imagery(
        input_path,
        gdal_cachemax=gdal_cachemax,
        target_crs=options.target_crs,
    )

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

    try:
        with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
            _, dst_w, dst_h = plan_destination_grid(src, parse_crs(options.target_crs))
            samples = destination_sample_count(
                src.samples,
                add_alpha=options.add_alpha,
                white_as_transparent=options.white_as_transparent,
            )
            reproject_b = raster_bytes(dst_w, dst_h, samples)
            overview_b = overview_bytes(dst_w, dst_h, samples) if options.build_overviews else 0
    except RasterError as exc:
        raise PreprocessError(str(exc)) from exc
    preprocess_bytes = max(1, reproject_b + overview_b)

    def _emit_reproject(sub_percent: float, message: str | None) -> None:
        if on_subprogress is None:
            return
        done = fraction_to_bytes(reproject_b, sub_percent)
        on_subprogress(100.0 * done / preprocess_bytes, message or "reproject")

    try:
        reproject_geotiff(
            input_path,
            final,
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
        _unlink_if_exists(final)
        raise PreprocessError(str(exc)) from exc

    if options.build_overviews:

        def _emit_overview(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            done = reproject_b + fraction_to_bytes(overview_b, sub_percent)
            on_subprogress(min(100.0 * done / preprocess_bytes, 100.0), message or "overview add")

        try:
            add_overviews(
                final,
                block_size=options.block_size,
                compress=compress if compress != "JPEG" else "DEFLATE",
                jpeg_quality=options.jpeg_quality,
                cache_bytes=cache_bytes,
                on_progress=_emit_overview if on_subprogress is not None else None,
            )
        except RasterError as exc:
            _unlink_if_exists(final)
            _unlink_if_exists(final_ovr)
            raise PreprocessError(str(exc)) from exc

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")
    return final
