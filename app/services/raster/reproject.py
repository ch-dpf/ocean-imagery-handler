"""Reproject a GeoTIFF to a target CRS (replacement for ``gdal raster reproject``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.crsutil import (
    WEB_MERCATOR_MAX_LAT,
    crs_epsg,
    destination_pixel_size,
    grid_dimension,
    make_transformer,
    parse_crs,
    transform_bounds,
)
from app.services.raster.errors import RasterError
from app.services.raster.geotiff import GeoTiffReader, write_geotiff_tiled
from app.services.raster.parallel import default_workers, ordered_parallel_map
from app.services.raster.warp import warp_window

ProgressFn = Callable[[float, str | None], None]


def destination_sample_count(
    src_samples: int,
    *,
    add_alpha: bool,
    white_as_transparent: bool,
) -> int:
    """uint8 band count written by ``reproject_geotiff``."""
    bands = min(int(src_samples), 4)
    if bands >= 4:
        return 4
    if bands == 2:
        return 2
    if add_alpha or white_as_transparent:
        return bands + 1
    return bands


def plan_destination_grid(
    src: GeoTiffReader,
    dst_crs: CRS,
) -> tuple[Affine, int, int]:
    src_bounds = src.bounds
    if crs_epsg(dst_crs) == 3857:
        wgs84 = parse_crs("EPSG:4326")
        west, south, east, north = transform_bounds(src.crs, wgs84, src_bounds)
        south = max(south, -WEB_MERCATOR_MAX_LAT)
        north = min(north, WEB_MERCATOR_MAX_LAT)
        left, bottom, right, top = transform_bounds(wgs84, dst_crs, (west, south, east, north))
    else:
        left, bottom, right, top = transform_bounds(src.crs, dst_crs, src_bounds)
    if not np.isfinite([left, bottom, right, top]).all() or right <= left or top <= bottom:
        raise RasterError("Destination extent is empty after reprojection")
    px, py = destination_pixel_size(src.crs, dst_crs, src.affine, src.width, src.height)
    width = grid_dimension(right - left, px)
    height = grid_dimension(top - bottom, py)
    if width > 2_000_000 or height > 2_000_000:
        raise RasterError(f"Destination raster too large: {width}x{height}")
    affine = Affine.north_up(left, top, (right - left) / width, (top - bottom) / height)
    return affine, width, height


def reproject_geotiff(
    input_path: Path,
    output_path: Path,
    *,
    dst_crs: str | CRS,
    compress: str,
    block_size: int,
    jpeg_quality: int,
    add_alpha: bool,
    white_as_transparent: bool,
    cache_bytes: int,
    resampling: str = "bilinear",
    workers: int | None = None,
    on_progress: ProgressFn | None = None,
) -> None:
    target = parse_crs(dst_crs)
    thread_count = default_workers() if workers is None else max(1, int(workers))
    with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
        dst_affine, dst_w, dst_h = plan_destination_grid(src, target)
        transformer = None if src.crs.equals(target) else make_transformer(target, src.crs)
        out_samples = destination_sample_count(
            src.samples,
            add_alpha=add_alpha,
            white_as_transparent=white_as_transparent,
        )

        tile = block_size
        n_ty = (dst_h + tile - 1) // tile
        n_tx = (dst_w + tile - 1) // tile
        coords = [(ty, tx) for ty in range(n_ty) for tx in range(n_tx)]
        planned_bytes = dst_w * dst_h * out_samples
        done_bytes = 0

        def _compute_tile(coord: tuple[int, int]) -> np.ndarray:
            ty, tx = coord
            r0 = ty * tile
            c0 = tx * tile
            sl_h = min(tile, dst_h - r0)
            sl_w = min(tile, dst_w - c0)
            warped = warp_window(
                src,
                dst_affine,
                target,
                r0,
                c0,
                sl_h,
                sl_w,
                resampling,
                add_alpha=add_alpha,
                white_as_transparent=white_as_transparent,
                transformer=transformer,
            )
            if warped.shape[2] < out_samples:
                padded = np.zeros((sl_h, sl_w, out_samples), dtype=np.uint8)
                padded[:, :, : warped.shape[2]] = warped
                if out_samples in {2, 4} and warped.shape[2] not in {2, 4}:
                    padded[:, :, -1] = 255
                warped = padded
            elif warped.shape[2] > out_samples:
                warped = warped[:, :, :out_samples]
            full = np.zeros((tile, tile, out_samples), dtype=np.uint8)
            full[:sl_h, :sl_w] = warped
            return full

        def tiles() -> Iterator[np.ndarray]:
            nonlocal done_bytes
            for coord, full in zip(coords, ordered_parallel_map(coords, _compute_tile, workers=thread_count)):
                ty, tx = coord
                sl_h = min(tile, dst_h - ty * tile)
                sl_w = min(tile, dst_w - tx * tile)
                done_bytes += sl_h * sl_w * out_samples
                if on_progress is not None:
                    percent = 100.0 if planned_bytes <= 0 else 100.0 * done_bytes / planned_bytes
                    on_progress(percent, "reproject")
                yield full

        write_geotiff_tiled(
            output_path,
            tiles(),
            shape=(dst_h, dst_w, out_samples),
            affine=dst_affine,
            crs=target,
            compress=compress,
            block_size=block_size,
            jpeg_quality=jpeg_quality,
        )
    if on_progress is not None:
        on_progress(100.0, "reproject complete")
