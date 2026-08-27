"""Invoke gdal raster tile for imagery tiling."""

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.schemas import ResamplingMethod, TileProfile, TileScheme, TilingOptions
from app.services.job_progress import gdal_progress_flag_unsupported, run_gdal_command

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
    *,
    show_progress: bool = False,
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
    else:
        # Auto max zoom: still build overview pyramid from end_zoom upward.
        cmd.extend(["--min-zoom", str(options.end_zoom)])

    if options.thread_count is not None:
        cmd.extend(["-j", str(options.thread_count)])
    if options.resume:
        cmd.append("--resume")
    if options.kml:
        cmd.append("--kml")
    if show_progress:
        cmd.append("--progress")
    elif not options.verbose:
        cmd.append("-q")

    cmd.extend([str(input_path), str(output_dir)])
    return cmd


def run_raster_tile(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> None:
    """Run gdal raster tile to produce tiles in the configured scheme (XYZ or TMS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GDAL_CACHEMAX": str(gdal_cachemax)}
    show_progress = on_subprogress is not None
    cmd = build_raster_tile_command(
        input_path,
        output_dir,
        options,
        show_progress=show_progress,
    )
    logger.info("Running gdal raster tile: %s", " ".join(cmd))
    try:
        run_gdal_command(cmd, env=env, on_subprogress=on_subprogress)
    except subprocess.CalledProcessError as exc:
        if show_progress and gdal_progress_flag_unsupported(exc.stderr or ""):
            logger.warning(
                "gdal raster tile does not support --progress on this GDAL build; retrying quietly"
            )
            fallback_cmd = build_raster_tile_command(
                input_path,
                output_dir,
                options,
                show_progress=False,
            )
            if on_subprogress is not None:
                on_subprogress(0.0, "Generating tiles")
            try:
                run_gdal_command(fallback_cmd, env=env, on_subprogress=on_subprogress)
            except subprocess.CalledProcessError as fallback_exc:
                raise TilerError(
                    f"gdal raster tile failed ({fallback_exc.returncode})\n"
                    f"stdout: {fallback_exc.output}\nstderr: {fallback_exc.stderr}"
                ) from fallback_exc
            if on_subprogress is not None:
                on_subprogress(100.0, "Tiling complete")
            return
        raise TilerError(
            f"gdal raster tile failed ({exc.returncode})\n"
            f"stdout: {exc.output}\nstderr: {exc.stderr}"
        ) from exc
