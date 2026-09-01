"""CRS parsing and bound transforms via pyproj (not GDAL)."""

from __future__ import annotations

import math

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

from app.services.raster.affine import Affine
from app.services.raster.errors import RasterError

WEB_MERCATOR_MAX_LAT = 85.05112878
EARTH_HALF = 20037508.342789244  # WGS84 a * pi


def parse_crs(value: str | CRS) -> CRS:
    if isinstance(value, CRS):
        return value
    try:
        return CRS.from_user_input(value)
    except CRSError as exc:
        raise RasterError(f"Unrecognized CRS: {value}") from exc


def crs_epsg(crs: CRS) -> int | None:
    code = crs.to_epsg()
    return int(code) if code is not None else None


def crs_equal(left: CRS, right: CRS) -> bool:
    if left.equals(right):
        return True
    a = crs_epsg(left)
    b = crs_epsg(right)
    return a is not None and a == b


def make_transformer(source: CRS, target: CRS) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


def transform_xy(
    transformer: Transformer,
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    try:
        out_x, out_y = transformer.transform(xs, ys, errcheck=False)
    except ProjError:
        out_x = np.full_like(xs, np.nan, dtype=np.float64)
        out_y = np.full_like(ys, np.nan, dtype=np.float64)
        return out_x, out_y
    out_x = np.asarray(out_x, dtype=np.float64)
    out_y = np.asarray(out_y, dtype=np.float64)
    return out_x, out_y


def densify_rect(
    left: float,
    bottom: float,
    right: float,
    top: float,
    density: int = 21,
) -> tuple[np.ndarray, np.ndarray]:
    density = max(2, density)
    edge = np.linspace(0.0, 1.0, density)
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    xs.append(left + edge * (right - left))
    ys.append(np.full(density, bottom))
    xs.append(np.full(density, right))
    ys.append(bottom + edge * (top - bottom))
    xs.append(left + edge * (right - left))
    ys.append(np.full(density, top))
    xs.append(np.full(density, left))
    ys.append(bottom + edge * (top - bottom))
    return np.concatenate(xs), np.concatenate(ys)


def transform_bounds(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
    *,
    density: int = 21,
) -> tuple[float, float, float, float]:
    """Return (left, bottom, right, top) in ``target`` CRS."""
    left, bottom, right, top = bounds
    if crs_equal(source, target):
        return left, bottom, right, top
    transformer = make_transformer(source, target)
    xs, ys = densify_rect(left, bottom, right, top, density)
    tx, ty = transform_xy(transformer, xs, ys)
    finite = np.isfinite(tx) & np.isfinite(ty)
    if not np.any(finite):
        raise RasterError("Failed to transform raster bounds between CRS")
    return float(np.min(tx[finite])), float(np.min(ty[finite])), float(np.max(tx[finite])), float(np.max(ty[finite]))


def clip_mercator_lat(lat: float) -> float:
    return min(max(lat, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)


def estimate_destination_pixel_size(
    src_crs: CRS,
    dst_crs: CRS,
    src_affine: Affine,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Approximate destination pixel size from a source pixel at the raster center."""
    col = max(width * 0.5, 0.0)
    row = max(height * 0.5, 0.0)
    x0, y0 = src_affine.xy(col, row)
    x1, y1 = src_affine.xy(col + 1.0, row)
    x2, y2 = src_affine.xy(col, row + 1.0)
    if crs_equal(src_crs, dst_crs):
        dx = math.hypot(float(x1) - float(x0), float(y1) - float(y0))
        dy = math.hypot(float(x2) - float(x0), float(y2) - float(y0))
        return max(dx, 1e-12), max(dy, 1e-12)

    transformer = make_transformer(src_crs, dst_crs)
    pts_x = np.array([x0, x1, x2], dtype=np.float64)
    pts_y = np.array([y0, y1, y2], dtype=np.float64)
    tx, ty = transform_xy(transformer, pts_x, pts_y)
    if not np.all(np.isfinite(tx) & np.isfinite(ty)):
        return src_affine.pixel_width, src_affine.pixel_height
    dx = math.hypot(float(tx[1] - tx[0]), float(ty[1] - ty[0]))
    dy = math.hypot(float(tx[2] - tx[0]), float(ty[2] - ty[0]))
    return max(dx, 1e-12), max(dy, 1e-12)


def wgs84_bounds_from_rect(
    crs: CRS,
    bounds: tuple[float, float, float, float],
) -> list[float]:
    left, bottom, right, top = transform_bounds(crs, parse_crs("EPSG:4326"), bounds)
    west = min(max(left, -180.0), 180.0)
    east = min(max(right, -180.0), 180.0)
    south = min(max(bottom, -90.0), 90.0)
    north = min(max(top, -90.0), 90.0)
    if west >= east or south >= north:
        return [west, south, east, north]
    return [west, south, east, north]
