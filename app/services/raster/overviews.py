"""Build reduced-resolution GeoTIFF overviews (replacement for ``gdal raster overview add``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import tifffile

from app.services.raster.geotiff import GeoTiffReader, geotiff_extratags, tiff_compression
from app.services.raster.parallel import default_workers, ordered_parallel_map
from app.services.raster.resample import resize_array, to_uint8

ProgressFn = Callable[[float, str | None], None]
DEFAULT_LEVELS = (2, 4, 8, 16)


def add_overviews(
    dataset: Path,
    *,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    block_size: int = 256,
    compress: str = "DEFLATE",
    jpeg_quality: int = 85,
    cache_bytes: int = 512 * 1024 * 1024,
    workers: int | None = None,
    on_progress: ProgressFn | None = None,
) -> Path | None:
    """Write ``dataset.tif.ovr`` with average-resampled pyramid levels."""
    ovr_path = Path(str(dataset) + ".ovr")
    if ovr_path.exists():
        ovr_path.unlink()

    with GeoTiffReader(dataset, cache_bytes=cache_bytes) as src:
        valid_levels = [level for level in levels if src.width // level >= 1 and src.height // level >= 1]
        if not valid_levels:
            return None
        thread_count = default_workers() if workers is None else max(1, int(workers))
        total_tiles = 0
        specs: list[tuple[int, int, int]] = []
        for level in valid_levels:
            out_w = max(1, src.width // level)
            out_h = max(1, src.height // level)
            n_ty = (out_h + block_size - 1) // block_size
            n_tx = (out_w + block_size - 1) // block_size
            specs.append((level, out_h, out_w))
            total_tiles += n_ty * n_tx
        total_tiles = max(1, total_tiles)
        done = 0
        codec, codec_args = tiff_compression(
            "DEFLATE" if compress.upper() == "JPEG" else compress,
            jpeg_quality,
        )

        with tifffile.TiffWriter(ovr_path, bigtiff=True) as tif:
            for level, out_h, out_w in specs:
                samples = src.samples if src.samples <= 4 else 4
                n_ty = (out_h + block_size - 1) // block_size
                n_tx = (out_w + block_size - 1) // block_size
                affine = src.affine.scaled(level)

                def tiles(
                    level: int = level,
                    out_h: int = out_h,
                    out_w: int = out_w,
                    n_ty: int = n_ty,
                    n_tx: int = n_tx,
                    samples: int = samples,
                ) -> Iterator[np.ndarray]:
                    nonlocal done
                    coords = [(ty, tx) for ty in range(n_ty) for tx in range(n_tx)]

                    def _compute_tile(coord: tuple[int, int]) -> np.ndarray:
                        ty, tx = coord
                        r0 = ty * block_size
                        c0 = tx * block_size
                        sl_h = min(block_size, out_h - r0)
                        sl_w = min(block_size, out_w - c0)
                        src_r0 = r0 * level
                        src_c0 = c0 * level
                        src_h = min(src.height - src_r0, sl_h * level)
                        src_w = min(src.width - src_c0, sl_w * level)
                        window = to_uint8(src.read_window(src_r0, src_c0, src_h, src_w))
                        if window.shape[2] > samples:
                            window = window[:, :, :samples]
                        resized = resize_array(window, sl_h, sl_w, "average")
                        if resized.shape[2] < samples:
                            padded = np.zeros((sl_h, sl_w, samples), dtype=np.uint8)
                            padded[:, :, : resized.shape[2]] = resized
                            resized = padded
                        full = np.zeros((block_size, block_size, samples), dtype=np.uint8)
                        full[:sl_h, :sl_w] = resized[:, :, :samples]
                        return full

                    for full in ordered_parallel_map(coords, _compute_tile, workers=thread_count):
                        done += 1
                        if on_progress is not None:
                            on_progress(100.0 * done / total_tiles, "overview add")
                        if samples == 1:
                            yield full[:, :, 0]
                        else:
                            yield full

                photometric = "minisblack" if samples <= 2 else "rgb"
                extrasamples = "unassalpha" if samples in {2, 4} else None
                write_shape: tuple[int, ...] = (out_h, out_w) if samples == 1 else (out_h, out_w, samples)
                kwargs: dict = {
                    "shape": write_shape,
                    "dtype": np.uint8,
                    "photometric": photometric,
                    "tile": (block_size, block_size),
                    "extratags": geotiff_extratags(src.crs, affine),
                    "software": "ocean-imagery-handler",
                    "metadata": None,
                }
                if extrasamples is not None:
                    kwargs["extrasamples"] = extrasamples
                if codec is not None:
                    kwargs["compression"] = codec
                    if codec_args:
                        kwargs["compressionargs"] = codec_args
                tif.write(tiles(), **kwargs)

    if on_progress is not None:
        on_progress(100.0, "overview complete")
    return ovr_path if ovr_path.is_file() else None
