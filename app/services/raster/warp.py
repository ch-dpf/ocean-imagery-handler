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
    remap_image,
    resize_array,
    to_uint8,
    upsample2d,
)

_MAX_SOURCE_WINDOW = 4096
_SPARSE_STEP = 8


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


def _north_up_colrow_maps(
    src_affine: Affine,
    dst_affine: Affine,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
) -> tuple[np.ndarray, np.ndarray]:
    col_1d = (
        dst_affine.a * (np.arange(dst_w, dtype=np.float64) + dst_col0 + 0.5) + dst_affine.c - src_affine.c
    ) / src_affine.a
    row_1d = (
        dst_affine.e * (np.arange(dst_h, dtype=np.float64) + dst_row0 + 0.5) + dst_affine.f - src_affine.f
    ) / src_affine.e
    return np.meshgrid(col_1d, row_1d)


def _src_colrow_maps(
    src: GeoTiffReader,
    dst_affine: Affine,
    dst_crs: CRS,
    dst_row0: int,
    dst_col0: int,
    dst_h: int,
    dst_w: int,
    transformer: Transformer | None,
) -> tuple[np.ndarray, np.ndarray]:
    if crs_equal(src.crs, dst_crs) and src.affine.is_north_up() and dst_affine.is_north_up():
        return _north_up_colrow_maps(src.affine, dst_affine, dst_row0, dst_col0, dst_h, dst_w)

    use_sparse = dst_h >= 32 and dst_w >= 32
    if use_sparse:
        n_r = max(2, dst_h // _SPARSE_STEP + 1)
        n_c = max(2, dst_w // _SPARSE_STEP + 1)
        row_s = np.linspace(dst_row0 + 0.5, dst_row0 + dst_h - 0.5, n_r)
        col_s = np.linspace(dst_col0 + 0.5, dst_col0 + dst_w - 0.5, n_c)
        rows, cols = np.meshgrid(row_s, col_s, indexing="ij")
    else:
        rows, cols = np.meshgrid(
            np.arange(dst_h, dtype=np.float64) + dst_row0 + 0.5,
            np.arange(dst_w, dtype=np.float64) + dst_col0 + 0.5,
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
    if use_sparse:
        src_cols = upsample2d(src_cols, dst_h, dst_w)
        src_rows = upsample2d(src_rows, dst_h, dst_w)
    return src_cols, src_rows


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
    src_cols, src_rows = _src_colrow_maps(
        src, dst_affine, dst_crs, dst_row0, dst_col0, dst_h, dst_w, transformer
    )
    finite = np.isfinite(src_cols) & np.isfinite(src_rows)
    inside = finite & (src_cols >= 0) & (src_cols < src.width) & (src_rows >= 0) & (src_rows < src.height)

    out_bands = min(src.samples, 4)
    empty = np.zeros((dst_h, dst_w, out_bands), dtype=np.uint8)
    if not np.any(inside):
        return _apply_alpha(
            empty, inside, add_alpha=add_alpha, white_as_transparent=white_as_transparent, source_alpha=None
        )

    pad = _pad_for_method(resampling)
    finite_rows = src_rows[finite]
    finite_cols = src_cols[finite]
    level = src.select_level(
        float(finite_cols.max() - finite_cols.min()),
        float(finite_rows.max() - finite_rows.min()),
        dst_w,
        dst_h,
    )
    if level.scale != 1:
        sx = level.width / src.width
        sy = level.height / src.height
        src_cols = src_cols * sx
        src_rows = src_rows * sy
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
        window = resize_array(
            to_uint8(src.read_window(rmin, cmin, win_h, win_w, level=level)),
            scaled_h,
            scaled_w,
            "average",
        )
        rel_x = (src_cols - cmin) / scale
        rel_y = (src_rows - rmin) / scale
    else:
        window = to_uint8(src.read_window(rmin, cmin, win_h, win_w, level=level))
        rel_x = src_cols - cmin
        rel_y = src_rows - rmin

    rgb = remap_image(window, rel_x, rel_y, resampling)
    source_alpha = rgb[:, :, -1] if rgb.shape[2] in {2, 4} else None
    if rgb.shape[2] in {2, 4}:
        color = rgb[:, :, :-1]
    elif rgb.shape[2] >= 3:
        color = rgb[:, :, :3]
    else:
        color = rgb
    return _apply_alpha(
        color if color.ndim == 3 else color[:, :, np.newaxis],
        inside,
        add_alpha=add_alpha,
        white_as_transparent=white_as_transparent,
        source_alpha=source_alpha,
    )
