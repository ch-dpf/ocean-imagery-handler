"""GDAL preprocessing pipeline for orthophoto imagery."""

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from app.schemas import PreprocessOptions
from app.services.job_progress import gdal_progress_flag_unsupported, run_gdal_command

logger = logging.getLogger(__name__)

GDAL_BIN = shutil.which("gdal")


class PreprocessError(RuntimeError):
    pass


def _creation_options(options: PreprocessOptions, compress: str) -> list[str]:
    # IF_SAFER: use BigTIFF when the raster may exceed the classic ~4GB TIFF limit
    # (common for provincial/high-zoom orthophotos during reproject + alpha).
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


def build_reproject_command(
    input_path: Path,
    output_path: Path,
    options: PreprocessOptions,
    *,
    compress: str | None = None,
    show_progress: bool = False,
) -> list[str]:
    """Build gdal raster reproject command line."""
    if GDAL_BIN is None:
        raise PreprocessError("gdal CLI not found; install GDAL >= 3.11")

    codec = (compress or options.compress).upper()
    cmd = [
        GDAL_BIN,
        "raster",
        "reproject",
        "--dst-crs",
        options.target_crs,
        "-r",
        "bilinear",
        "--overwrite",
        "-j",
        "ALL_CPUS",
    ]

    for creation_option in _creation_options(options, codec):
        cmd.extend(["--co", creation_option])

    if options.white_as_transparent:
        cmd.extend(["--add-alpha", "--src-nodata", "255 255 255"])
    elif options.add_alpha:
        cmd.append("--add-alpha")

    if show_progress:
        cmd.append("--progress")

    cmd.extend([str(input_path), str(output_path)])
    return cmd


def build_raster_info_command(
    dataset: Path,
    *,
    output_format: Literal["text", "json"] = "text",
) -> list[str]:
    """Build gdal raster info command line."""
    if GDAL_BIN is None:
        raise PreprocessError("gdal CLI not found; install GDAL >= 3.11")

    cmd = [GDAL_BIN, "raster", "info"]
    if output_format == "json":
        cmd.extend(["--format", "JSON"])
    cmd.append(str(dataset))
    return cmd


def build_overview_add_command(dataset: Path, *, show_progress: bool = False) -> list[str]:
    """Build gdal raster overview add command line."""
    if GDAL_BIN is None:
        raise PreprocessError("gdal CLI not found; install GDAL >= 3.11")

    cmd = [
        GDAL_BIN,
        "raster",
        "overview",
        "add",
        "-r",
        "average",
        "--levels=2,4,8,16",
    ]
    if show_progress:
        cmd.append("--progress")
    cmd.append(str(dataset))
    return cmd


def _run_gdal(
    cmd: list[str],
    env: dict[str, str],
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
    quiet_cmd: list[str] | None = None,
) -> None:
    """Run a GDAL raster subcommand, streaming --progress output when enabled."""
    logger.info("Running: %s", " ".join(cmd))
    show_progress = on_subprogress is not None and "--progress" in cmd
    try:
        run_gdal_command(cmd, env=env, on_subprogress=on_subprogress)
    except subprocess.CalledProcessError as exc:
        if show_progress and quiet_cmd is not None and gdal_progress_flag_unsupported(exc.stderr or ""):
            logger.warning(
                "GDAL command does not support --progress on this build; retrying without progress"
            )
            if on_subprogress is not None:
                on_subprogress(0.0, None)
            try:
                run_gdal_command(quiet_cmd, env=env, on_subprogress=on_subprogress)
            except subprocess.CalledProcessError as fallback_exc:
                raise PreprocessError(
                    f"Command failed ({fallback_exc.returncode}): {' '.join(quiet_cmd)}\n"
                    f"stdout: {fallback_exc.output}\nstderr: {fallback_exc.stderr}"
                ) from fallback_exc
            return
        raise PreprocessError(
            f"Command failed ({exc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {exc.output}\nstderr: {exc.stderr}"
        ) from exc


def gdal_info(dataset: Path, env: dict[str, str] | None = None) -> str:
    """Return human-readable raster metadata via gdal raster info."""
    cmd = build_raster_info_command(dataset, output_format="text")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise PreprocessError(f"gdal raster info failed: {result.stderr}")
    return result.stdout


def _raster_info_json(dataset: Path, env: dict[str, str] | None = None) -> dict:
    cmd = build_raster_info_command(dataset, output_format="json")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise PreprocessError(f"gdal raster info --format=JSON failed: {result.stderr}")
    return json.loads(result.stdout)


def _bounds_from_wgs84_extent(data: dict) -> list[float] | None:
    """Extract [west, south, east, north] from gdal raster info JSON wgs84Extent."""
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
    data = _raster_info_json(dataset, env)

    bounds = _bounds_from_wgs84_extent(data)
    if bounds is not None:
        return bounds

    corners = data.get("cornerCoordinates", {})
    lower_left = corners.get("lowerLeft")
    upper_right = corners.get("upperRight")
    if not lower_left or not upper_right:
        return [-180.0, -90.0, 180.0, 90.0]

    wkt = data.get("coordinateSystem", {}).get("wkt", "")
    is_geographic = ("GEOGCRS" in wkt or "GEOGCS" in wkt) and "PROJCRS" not in wkt and "PROJCS" not in wkt
    if is_geographic:
        bounds = [
            float(lower_left[0]),
            float(lower_left[1]),
            float(upper_right[0]),
            float(upper_right[1]),
        ]
        if _bounds_valid_wgs84(bounds):
            return bounds

    source_srs = wkt or "EPSG:4326"
    corner_points = [
        corners.get("lowerLeft"),
        corners.get("lowerRight"),
        corners.get("upperRight"),
        corners.get("upperLeft"),
    ]
    corner_points = [point for point in corner_points if point]
    if not corner_points:
        return [-180.0, -90.0, 180.0, 90.0]

    transform_input = "\n".join(f"{point[0]} {point[1]}" for point in corner_points)
    result = subprocess.run(
        [
            "gdaltransform",
            "-s_srs",
            source_srs,
            "-t_srs",
            "EPSG:4326",
        ],
        input=transform_input,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return [-180.0, -90.0, 180.0, 90.0]

    transformed: list[tuple[float, float]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            transformed.append((float(parts[0]), float(parts[1])))

    if not transformed:
        return [-180.0, -90.0, 180.0, 90.0]

    lons = [point[0] for point in transformed]
    lats = [point[1] for point in transformed]
    return [min(lons), min(lats), max(lons), max(lats)]


def preprocess_imagery(
    input_path: Path,
    work_dir: Path,
    options: PreprocessOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> Path:
    """Run GDAL preprocessing and return path to tiling-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GDAL_CACHEMAX": str(gdal_cachemax)}

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

    show_progress = on_subprogress is not None

    def _emit_reproject(sub_percent: float, message: str | None) -> None:
        if on_subprogress is None:
            return
        scaled = sub_percent * warp_weight if options.build_overviews else sub_percent
        on_subprogress(scaled, message or "gdal raster reproject")

    reproject_cmd = build_reproject_command(
        input_path, warped, options, compress=compress, show_progress=show_progress
    )
    reproject_quiet = build_reproject_command(input_path, warped, options, compress=compress)
    _run_gdal(
        reproject_cmd,
        env,
        on_subprogress=_emit_reproject,
        quiet_cmd=reproject_quiet if show_progress else None,
    )

    if options.build_overviews:

        def _emit_overview(sub_percent: float, message: str | None) -> None:
            if on_subprogress is None:
                return
            scaled = warp_weight * 100.0 + sub_percent * addo_weight
            on_subprogress(min(scaled, 100.0), message or "gdal raster overview add")

        overview_cmd = build_overview_add_command(warped, show_progress=show_progress)
        overview_quiet = build_overview_add_command(warped)
        _run_gdal(
            overview_cmd,
            env,
            on_subprogress=_emit_overview,
            quiet_cmd=overview_quiet if show_progress else None,
        )

    if on_subprogress is not None:
        on_subprogress(100.0, "preprocess complete")

    shutil.copy2(warped, final)
    return final
