"""Job progress measured in uncompressed raster bytes (uint8 pixels × bands)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.schemas import PreprocessOptions, TilingOptions
from app.services.raster.crsutil import parse_crs
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.overviews import DEFAULT_LEVELS
from app.services.raster.reproject import destination_sample_count, plan_destination_grid
from app.services.raster.tiles import RasterExtent, count_output_tiles, tile_sample_count


def raster_bytes(width: int, height: int, samples: int) -> int:
    return max(0, int(width) * int(height) * int(samples))


def overview_bytes(
    width: int,
    height: int,
    samples: int,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
) -> int:
    total = 0
    for level in levels:
        out_w = width // level
        out_h = height // level
        if out_w >= 1 and out_h >= 1:
            total += raster_bytes(out_w, out_h, samples)
    return total


def fraction_to_bytes(span: int, percent: float) -> int:
    """Map a 0-100 fraction of ``span`` bytes; 100% is exactly ``span``."""
    if span <= 0:
        return 0
    if percent >= 100.0:
        return span
    if percent <= 0.0:
        return 0
    return min(span, int(span * percent / 100.0))


@dataclass(frozen=True, slots=True)
class ByteBudget:
    reproject: int
    overviews: int
    tiles: int

    @property
    def preprocess(self) -> int:
        return self.reproject + self.overviews

    @property
    def total(self) -> int:
        return self.reproject + self.overviews + self.tiles


def plan_pipeline_bytes(
    input_path: Path,
    preprocess: PreprocessOptions,
    tiling: TilingOptions,
    *,
    cache_bytes: int,
) -> ByteBudget:
    """Count uint8 raster bytes the job will write (reproject + overviews + tiles)."""
    with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
        dst_crs = parse_crs(preprocess.target_crs)
        affine, width, height = plan_destination_grid(src, dst_crs)
        samples = destination_sample_count(
            src.samples,
            add_alpha=preprocess.add_alpha,
            white_as_transparent=preprocess.white_as_transparent,
        )
        reproject = raster_bytes(width, height, samples)
        overviews = overview_bytes(width, height, samples) if preprocess.build_overviews else 0
        extent = RasterExtent(
            crs=dst_crs,
            affine=affine,
            width=width,
            height=height,
            samples=samples,
        )
        n_tiles = count_output_tiles(extent, tiling)
        tile_samples = tile_sample_count(tiling.profile, samples)
        tiles = n_tiles * raster_bytes(tiling.tile_size, tiling.tile_size, tile_samples)
        return ByteBudget(reproject=reproject, overviews=overviews, tiles=tiles)
