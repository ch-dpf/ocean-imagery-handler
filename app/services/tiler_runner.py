"""Invoke gdal raster tile for imagery tiling."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.schemas import ResamplingMethod, TileProfile, TileScheme, TilingOptions

logger = logging.getLogger(__name__)

GDAL_BIN = shutil.which("gdal")

# Map API profile names to gdal raster tile --tiling-scheme values.
PROFILE_TO_TILING_SCHEME: dict[TileProfile, str] = {
    TileProfile.MERCATOR: "WebMercatorQuad",
    TileProfile.GEODETIC: "WorldCRS84Quad",
    TileProfile.RASTER: "raster",
}

# Map API resampling names to gdal raster tile -r values.
# gdal2tiles used "near"; gdal raster tile expects "nearest".
# "antialias" is not supported by gdal raster tile; map to lanczos.
RESAMPLING_TO_GDAL: dict[ResamplingMethod, str] = {
    ResamplingMethod.NEAREST: "nearest",
    ResamplingMethod.BILINEAR: "bilinear",
    ResamplingMethod.CUBIC: "cubic",
    ResamplingMethod.CUBICSPLINE: "cubicspline",
    ResamplingMethod.LANCZOS: "lanczos",
    ResamplingMethod.ANTIALIAS: "lanczos",
    ResamplingMethod.AVERAGE: "average",
    ResamplingMethod.MODE: "mode",
}


class TilerError(RuntimeError):
    pass


def build_raster_tile_command(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
) -> list[str]:
    """Build gdal raster tile command line."""
    if GDAL_BIN is None:
        raise TilerError("gdal CLI not found; install GDAL >= 3.11 (gdal raster tile)")

    tiling_scheme = PROFILE_TO_TILING_SCHEME[options.profile]
    resampling = RESAMPLING_TO_GDAL[options.resampling_method]

    cmd = [
        GDAL_BIN,
        "raster",
        "tile",
        "--tiling-scheme",
        tiling_scheme,
        "--format",
        options.tile_format.value,
        "-r",
        resampling,
        "--tile-size",
        str(options.tile_size),
        "--convention",
        options.tile_scheme.value,
        "--webviewer",
        "none",
    ]

    if options.start_zoom is not None:
        cmd.extend(["--min-zoom", str(options.end_zoom)])
        cmd.extend(["--max-zoom", str(options.start_zoom)])

    if options.thread_count is not None:
        cmd.extend(["-j", str(options.thread_count)])
    if options.resume:
        cmd.append("--resume")
    if options.kml:
        cmd.append("--kml")
    if not options.verbose:
        cmd.append("-q")

    cmd.extend([str(input_path), str(output_dir)])
    return cmd


def run_raster_tile(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    gdal_cachemax: int,
) -> None:
    """Run gdal raster tile to produce tiles in the configured scheme (XYZ or TMS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GDAL_CACHEMAX": str(gdal_cachemax)}
    cmd = build_raster_tile_command(input_path, output_dir, options)
    logger.info("Running gdal raster tile: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise TilerError(
            f"gdal raster tile failed ({result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
