"""GDAL preprocessing pipeline for orthophoto imagery."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

from app.schemas import PreprocessOptions

logger = logging.getLogger(__name__)


class PreprocessError(RuntimeError):
    pass


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise PreprocessError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def gdal_info(dataset: Path) -> str:
    result = subprocess.run(
        ["gdalinfo", str(dataset)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PreprocessError(f"gdalinfo failed: {result.stderr}")
    return result.stdout


def _gdalinfo_json(dataset: Path, env: dict[str, str] | None = None) -> dict:
    result = subprocess.run(
        ["gdalinfo", "-json", str(dataset)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise PreprocessError(f"gdalinfo -json failed: {result.stderr}")
    return json.loads(result.stdout)


def _bounds_from_wgs84_extent(data: dict) -> list[float] | None:
    """Extract [west, south, east, north] from gdalinfo -json wgs84Extent."""
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
    data = _gdalinfo_json(dataset, env)

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
) -> Path:
    """Run GDAL preprocessing and return path to tiling-ready raster."""
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {"GDAL_CACHEMAX": str(gdal_cachemax)}

    warped = work_dir / "warped.tif"
    final = work_dir / "preprocessed.tif"

    create_opts = [
        "-co",
        "TILED=YES",
        "-co",
        f"BLOCKXSIZE={options.block_size}",
        "-co",
        f"BLOCKYSIZE={options.block_size}",
    ]

    compress = options.compress.upper()
    needs_alpha = options.add_alpha or options.white_as_transparent
    if needs_alpha and compress == "JPEG":
        # JPEG cannot store an alpha band; keep transparency by switching codec.
        logger.warning("compress=JPEG is incompatible with alpha transparency; using DEFLATE instead")
        compress = "DEFLATE"

    create_opts.extend(["-co", f"COMPRESS={compress}"])
    if compress == "JPEG":
        create_opts.extend(["-co", f"JPEG_QUALITY={options.jpeg_quality}"])

    # Single-pass warp: white fill → transparent alpha (no separate nearblack tempfile).
    # nearblack -white on large orthos is very slow and can flood-fill valid imagery.
    warp_cmd = [
        "gdalwarp",
        "-t_srs",
        options.target_crs,
        "-r",
        "bilinear",
        *create_opts,
        str(input_path),
        str(warped),
    ]

    if options.white_as_transparent:
        # Exact white fill only (RGB 255,255,255). gdalwarp srcnodata cannot express a range;
        # near_white is reserved for future tolerance support and currently ignored here.
        if options.near_white > 0:
            logger.warning(
                "near_white=%s is ignored; only exact RGB(255,255,255) is treated as transparent",
                options.near_white,
            )
        warp_cmd[1:1] = [
            "-srcnodata",
            "255 255 255",
            "-dstalpha",
            "-wo",
            "UNIFIED_SRC_NODATA=YES",
        ]
    elif options.add_alpha:
        warp_cmd[1:1] = ["-dstalpha"]

    _run(warp_cmd, env=env)

    if options.build_overviews:
        _run(["gdaladdo", "-r", "average", str(warped), "2", "4", "8", "16"], env=env)

    shutil.copy2(warped, final)
    return final
