"""In-process GDAL preprocessing pipeline for orthophoto imagery."""

import logging
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from app.schemas import PreprocessOptions
from app.services.gdal_runtime import (
    GdalPythonError,
    finalize_algorithm,
    require_osr,
    run_algorithm,
)

logger = logging.getLogger(__name__)


class PreprocessError(RuntimeError):
    pass


def _creation_options(options: PreprocessOptions, compress: str) -> list[str]:
    creation_options = [
        "TILED=YES",
        f"BLOCKXSIZE={options.block_size}",
        f"BLOCKYSIZE={options.block_size}",
        f"COMPRESS={compress}",
        "BIGTIFF=IF_SAFER",
    ]
    if compress == "JPEG":
        creation_options.append(f"JPEG_QUALITY={options.jpeg_quality}")
    return creation_options


def build_reproject_arguments(
    input_path: Path,
    output_path: Path,
    options: PreprocessOptions,
    *,
    compress: str | None = None,
) -> dict[str, object]:
    """Build arguments for the GDAL raster-reproject algorithm."""
    codec = (compress or options.compress).upper()
    arguments: dict[str, object] = {
        "input": str(input_path),
        "output": str(output_path),
        "dst-crs": options.target_crs,
        "resampling": "bilinear",
        "overwrite": True,
        "num-threads": "ALL_CPUS",
        "creation-option": _creation_options(options, codec),
    }

    if options.white_as_transparent:
        arguments["add-alpha"] = True
        arguments["src-nodata"] = [255.0, 255.0, 255.0]
    elif options.add_alpha:
        arguments["add-alpha"] = True
    return arguments


def build_raster_info_arguments(
    dataset: Path,
    *,
    output_format: Literal["text", "json"] = "text",
) -> dict[str, object]:
    """Build arguments for the GDAL raster-info algorithm."""
    return {"input": str(dataset), "output-format": output_format}


def build_overview_add_arguments(dataset: Path) -> dict[str, object]:
    """Build arguments for the GDAL overview-add algorithm."""
    return {
        "input": str(dataset),
        "resampling": "average",
        "levels": [2, 4, 8, 16],
    }


def _config_from_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Extract GDAL-related settings from a legacy environment mapping."""
    prefixes = ("GDAL_", "CPL_", "PROJ_")
    return {
        str(key): str(value)
        for key, value in (env or {}).items()
        if str(key).startswith(prefixes)
    }


def _run_preprocess_algorithm(
    algorithm_path: list[str],
    arguments: Mapping[str, object],
    *,
    config: Mapping[str, str] | None = None,
    on_subprogress: Callable[[float, str | None], None] | None = None,
):
    try:
        return run_algorithm(
            algorithm_path,
            arguments,
            config=config,
            on_progress=on_subprogress,
        )
    except GdalPythonError as exc:
        raise PreprocessError(str(exc)) from exc


def _raster_info(
    dataset: Path,
    *,
    output_format: Literal["text", "json"],
    env: Mapping[str, str] | None = None,
):
    algorithm = _run_preprocess_algorithm(
        ["raster", "info"],
        build_raster_info_arguments(dataset, output_format=output_format),
        config=_config_from_env(env),
    )
    try:
        return algorithm.Output()
    finally:
        finalize_algorithm(algorithm)


def gdal_info(dataset: Path, env: Mapping[str, str] | None = None) -> str:
    """Return human-readable raster metadata through the GDAL Python API."""
    output = _raster_info(dataset, output_format="text", env=env)
    return output if isinstance(output, str) else str(output)


def _raster_info_json(dataset: Path, env: Mapping[str, str] | None = None) -> dict:
    output = _raster_info(dataset, output_format="json", env=env)
    if not isinstance(output, dict):
        raise PreprocessError("GDAL raster info returned an invalid JSON result")
    return output


def _bounds_from_wgs84_extent(data: dict) -> list[float] | None:
    """Extract [west, south, east, north] from raster-info wgs84Extent."""
    extent = data.get("wgs84Extent")
    if not extent:
        return None
    coordinates = extent.get("coordinates")
    if not coordinates or not coordinates[0]:
        return None

    ring = coordinates[0]
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


def _transform_points_to_wgs84(
    points: list[list[float] | tuple[float, ...]], source_srs: str
) -> list[tuple[float, float]]:
    """Transform corner points with OSR instead of invoking gdaltransform."""
    osr = require_osr()
    source = osr.SpatialReference()
    target = osr.SpatialReference()
    source.SetFromUserInput(source_srs)
    target.SetFromUserInput("EPSG:4326")
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source, target)
    return [
        tuple(transform.TransformPoint(float(point[0]), float(point[1])))[:2]
        for point in points
    ]


def parse_wgs84_bounds(
    dataset: Path, env: Mapping[str, str] | None = None
) -> list[float]:
    """Return [west, south, east, north] in WGS84."""
    data = _raster_info_json(dataset, env)

    bounds = _bounds_from_wgs84_extent(data)
    if bounds is not None and _bounds_valid_wgs84(bounds):
        return bounds

    corners = data.get("cornerCoordinates", {})
    lower_left = corners.get("lowerLeft")
    upper_right = corners.get("upperRight")
    if not lower_left or not upper_right:
        return [-180.0, -90.0, 180.0, 90.0]

    wkt = data.get("coordinateSystem", {}).get("wkt", "")
    is_geographic = (
        ("GEOGCRS" in wkt or "GEOGCS" in wkt)
        and "PROJCRS" not in wkt
        and "PROJCS" not in wkt
    )
    if is_geographic:
        bounds = [
            float(lower_left[0]),
            float(lower_left[1]),
            float(upper_right[0]),
            float(upper_right[1]),
        ]
        if _bounds_valid_wgs84(bounds):
            return bounds

    corner_points = [
        corners.get("lowerLeft"),
        corners.get("lowerRight"),
        corners.get("upperRight"),
        corners.get("upperLeft"),
    ]
    corner_points = [point for point in corner_points if point]
    if not corner_points:
        return [-180.0, -90.0, 180.0, 90.0]

    try:
        transformed = _transform_points_to_wgs84(corner_points, wkt or "EPSG:4326")
    except (GdalPythonError, RuntimeError, ValueError):
        logger.exception("Unable to transform raster bounds to WGS84")
        return [-180.0, -90.0, 180.0, 90.0]

    if not transformed:
        return [-180.0, -90.0, 180.0, 90.0]

    lons = [point[0] for point in transformed]
    lats = [point[1] for point in transformed]
    bounds = [min(lons), min(lats), max(lons), max(lats)]
    return bounds if _bounds_valid_wgs84(bounds) else [-180.0, -90.0, 180.0, 90.0]


def preprocess_imagery(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> Path:
    """Run in-process GDAL preprocessing and return a tiling-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)
    config = {"GDAL_CACHEMAX": str(gdal_cachemax)}
    warped = work_dir / "warped.tif"
    final = work_dir / "preprocessed.tif"

    compress = options.compress.upper()
    needs_alpha = options.add_alpha or options.white_as_transparent
    if needs_alpha and compress == "JPEG":
        logger.warning(
            "compress=JPEG is incompatible with alpha transparency; using DEFLATE instead"
        )
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
        scaled = sub_percent * warp_weight if options.build_overviews else sub_percent
        on_subprogress(scaled, message or "gdal raster reproject")

    reproject = _run_preprocess_algorithm(
        ["raster", "reproject"],
        build_reproject_arguments(input_path, warped, options, compress=compress),
        config=config,
        on_subprogress=_emit_reproject if on_subprogress is not None else None,
    )
    finalize_algorithm(reproject)

    if options.build_overviews:

        def _emit_overview(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            scaled = warp_weight * 100.0 + sub_percent * addo_weight
            on_subprogress(min(scaled, 100.0), message or "gdal raster overview add")

        overview = _run_preprocess_algorithm(
            ["raster", "overview", "add"],
            build_overview_add_arguments(warped),
            config=config,
            on_subprogress=_emit_overview if on_subprogress is not None else None,
        )
        finalize_algorithm(overview)

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")

    shutil.copy2(warped, final)
    return final
