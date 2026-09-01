"""Generate XYZ/TMS imagery tiles (Python raster engine, no GDAL CLI)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.schemas import ResamplingMethod, TileProfile, TilingOptions
from app.services.raster.errors import RasterError
from app.services.raster.tiles import generate_tiles

logger = logging.getLogger(__name__)

# Kept for API/docs compatibility with the former GDAL tiling-scheme names.
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


def _cache_bytes(gdal_cachemax: int | None) -> int:
    megabytes = max(int(gdal_cachemax or 64), 1)
    return megabytes * 1024 * 1024


def run_raster_tile(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    gdal_cachemax: int,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
) -> None:
    """Produce tiles in the configured scheme (XYZ or TMS)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Tiling %s -> %s (%s, %s, zoom %s-%s)",
        input_path,
        output_dir,
        options.profile.value,
        options.tile_scheme.value,
        options.end_zoom,
        options.start_zoom,
    )
    try:
        generate_tiles(
            input_path,
            output_dir,
            options,
            cache_bytes=_cache_bytes(gdal_cachemax),
            on_progress=on_subprogress,
        )
    except RasterError as exc:
        raise TilerError(str(exc)) from exc
