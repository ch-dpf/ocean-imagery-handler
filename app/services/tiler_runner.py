"""Invoke gdal2tiles for imagery tiling."""

import logging
import shutil
import subprocess
from pathlib import Path

from app.schemas import TileScheme, TilingOptions

logger = logging.getLogger(__name__)

GDAL2TILES_BIN = shutil.which("gdal2tiles.py") or shutil.which("gdal2tiles")


class TilerError(RuntimeError):
    pass


def build_gdal2tiles_command(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
) -> list[str]:
    """Build gdal2tiles command line."""
    if GDAL2TILES_BIN is None:
        raise TilerError("gdal2tiles.py not found; install gdal-bin")

    cmd = [
        GDAL2TILES_BIN,
        "-p",
        options.profile.value,
        "--tiledriver",
        options.tile_format.value,
        "-r",
        options.resampling_method.value,
        "--tilesize",
        str(options.tile_size),
        "-w",
        "none",
    ]

    if options.tile_scheme == TileScheme.XYZ:
        cmd.append("--xyz")

    if options.start_zoom is not None:
        cmd.extend(["--zoom", f"{options.end_zoom}-{options.start_zoom}"])

    if options.thread_count is not None:
        cmd.extend(["--processes", str(options.thread_count)])
    if options.resume:
        cmd.append("--resume")
    if options.verbose:
        cmd.append("--verbose")
    if options.kml:
        cmd.append("--force-kml")

    cmd.extend([str(input_path), str(output_dir)])
    return cmd


def run_gdal2tiles(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    gdal_cachemax: int,
) -> None:
    """Run gdal2tiles to produce tiles in the configured scheme (XYZ or TMS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    env = {"GDAL_CACHEMAX": str(gdal_cachemax)}
    cmd = build_gdal2tiles_command(input_path, output_dir, options)
    logger.info("Running gdal2tiles: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise TilerError(
            f"gdal2tiles failed ({result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
