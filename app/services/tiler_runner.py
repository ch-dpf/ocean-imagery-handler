"""Run GDAL's raster tile algorithm in the worker process."""

import logging
from collections.abc import Callable
from pathlib import Path

from app.schemas import ResamplingMethod, TileProfile, TilingOptions
from app.services.gdal_runtime import GdalPythonError, finalize_algorithm, run_algorithm

logger = logging.getLogger(__name__)

PROFILE_TO_TILING_SCHEME: dict[TileProfile, str] = {
    TileProfile.MERCATOR: "WebMercatorQuad",
    TileProfile.GEODETIC: "WorldCRS84Quad",
    TileProfile.RASTER: "raster",
}

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


def build_raster_tile_arguments(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
) -> dict[str, object]:
    """Build arguments for ``gdal.Run(['raster', 'tile'], ...)``."""
    arguments: dict[str, object] = {
        "input": str(input_path),
        "output": str(output_dir),
        "tiling-scheme": PROFILE_TO_TILING_SCHEME[options.profile],
        "output-format": options.tile_format.value,
        "resampling": RESAMPLING_TO_GDAL[options.resampling_method],
        "tile-size": options.tile_size,
        "convention": options.tile_scheme.value,
        "webviewer": ["none"],
        "min-zoom": options.end_zoom,
    }

    if options.start_zoom is not None:
        arguments["max-zoom"] = options.start_zoom
    if options.thread_count is not None:
        arguments["num-threads"] = str(options.thread_count)
    if options.resume:
        arguments["resume"] = True
    if options.kml:
        arguments["kml"] = True
    return arguments


def run_raster_tile(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> None:
    """Generate imagery tiles through GDAL's in-process algorithm API."""
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        algorithm = run_algorithm(
            ["raster", "tile"],
            build_raster_tile_arguments(input_path, output_dir, options),
            config={"GDAL_CACHEMAX": str(gdal_cachemax)},
            on_progress=on_subprogress,
        )
    except GdalPythonError as exc:
        raise TilerError(str(exc)) from exc
    finalize_algorithm(algorithm)

    if on_subprogress is not None:
        on_subprogress(100.0, "Tiling complete")
