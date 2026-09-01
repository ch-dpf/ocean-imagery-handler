"""Inverse-map warp from a GeoTIFF onto a destination pixel grid."""

from __future__ import annotations

import numpy as np
from pyproj import CRS, Transformer

from app.services.raster.affine import Affine
from app.services.raster.crsutil import crs_equal, make_transformer, transform_xy
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.resample import (
    RESAMPLE_CUBIC,
    RESAMPLE_LANCZOS,
    normalize_resampling,
    resize_array,
    sample_image,
    to_uint8,
)

_MAX_SOURCE_WINDOW = 4096


def _pad_for_method(method: str) -> int:
    kind = normalize_resampling(method)
    if kind == RESAMPLE_LANCZOS:
        return 3
    if kind == RESAMPLE_CUBIC:
        return 2
    return 1


def _apply_alpha(
    rgb: np.ndarray,
    valid: np.ndarray,
    *,
    add_alpha: bool,
    white_as_transparent: bool,
    source_alpha: np.ndarray | None,
) -> np.ndarray:
    bands = rgb.shape[2]
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    if source_alpha is not None:
        alpha = np.minimum(alpha, source_alpha)
    if white_as_transparent and bands >= 3:
        white = (rgb[:, :, 0] == 255) & (rgb[:, :, 1] == 255) & (rgb[:, :, 2] == 255)
        alpha = np.where(white, 0, alpha).astype(np.uint8)
    need_alpha = add_alpha or white_as_transparent or source_alpha is not None
    if not need_alpha:
        return rgb
    if bands in {2, 4}:
        rgb[:, :, -1] = alpha
        return rgb
    return np.concatenate([rgb, alpha[:, :, np.newaxis]], axis=2)


def warp_window(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    resampling: str,
    *,
    add_alpha: bool = False,
    white_as_transparent: bool = False,
    transformer: Transformer | None = None,
) -> np.ndarray:
    """Warp a destination window to uint8 HWC, optionally with alpha."""
    rows, cols = np.meshgrid(
        np.arange(dst_row0, dst_row0 + dst_h, dtype=np.float64) + 0.5,
        np.arange(dst_col0, dst_col0 + dst_w, dtype=np.float64) + 0.5,
        indexing="ij",
    )
    dst_x, dst_y = dst_affine.xy(cols, rows)
    if crs_equal(src.crs, dst_crs):
        src_x = np.asarray(dst_x, dtype=np.float64)
        src_y = np.asarray(dst_y, dtype=np.float64)
    else:
        xform = transformer or make_transformer(dst_crs, src.crs)
        src_x, src_y = transform_xy(xform, np.asarray(dst_x, dtype=np.float64), np.asarray(dst_y, dtype=np.float64))

    src_cols, src_rows = src.affine.colrow(src_x, src_y)
    src_cols = np.asarray(src_cols, dtype=np.float64)
    src_rows = np.asarray(src_rows, dtype=np.float64)
    finite = np.isfinite(src_cols) & np.isfinite(src_rows)
    inside = finite & (src_cols >= 0) & (src_cols < src.width) & (src_rows >= 0) & (src_rows < src.height)

    out_bands = src.samples
    empty = np.zeros((dst_h, dst_w, out_bands), dtype=np.uint8)
    if not np.any(inside):
        return _apply_alpha(empty, inside, add_alpha=add_alpha, white_as_transparent=white_as_transparent, source_alpha=None)

    pad = _pad_for_method(resampling)
    finite_rows = src_rows[finite]
    finite_cols = src_cols[finite]
    rmin = int(np.floor(finite_rows.min())) - pad
    rmax = int(np.ceil(finite_rows.max())) + pad + 1
    cmin = int(np.floor(finite_cols.min())) - pad
    cmax = int(np.ceil(finite_cols.max())) + pad + 1
    win_h = max(1, rmax - rmin)
    win_w = max(1, cmax - cmin)

    if win_h > _MAX_SOURCE_WINDOW or win_w > _MAX_SOURCE_WINDOW:
        scale = max(win_h / _MAX_SOURCE_WINDOW, win_w / _MAX_SOURCE_WINDOW)
        scaled_h = max(1, int(round(win_h / scale)))
        scaled_w = max(1, int(round(win_w / scale)))
        window = src.read_window(rmin, cmin, win_h, win_w)
        window = resize_array(to_uint8(window), scaled_h, scaled_w, "average")
        rel_rows = (src_rows - rmin) / scale
        rel_cols = (src_cols - cmin) / scale
    else:
        window = to_uint8(src.read_window(rmin, cmin, win_h, win_w))
        rel_rows = src_rows - rmin
        rel_cols = src_cols - cmin

    sampled = sample_image(window, rel_rows, rel_cols, resampling)
    rgb = np.clip(np.rint(sampled), 0, 255).astype(np.uint8)
    source_alpha = rgb[:, :, -1] if rgb.shape[2] in {2, 4} else None
    color = rgb[:, :, :3] if rgb.shape[2] >= 3 else rgb
    if rgb.shape[2] in {2, 4}:
        color = rgb[:, :, :-1]
        source_alpha = rgb[:, :, -1]
    return _apply_alpha(
        color if color.ndim == 3 else color[:, :, np.newaxis],
        inside,
        add_alpha=add_alpha,
        white_as_transparent=white_as_transparent,
        source_alpha=source_alpha,
    )
