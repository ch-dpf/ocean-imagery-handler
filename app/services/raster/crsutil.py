"""CRS parsing and bound transforms via pyproj (not GDAL)."""

from __future__ import annotations

import math

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

from app.services.raster.affine import Affine
from app.services.raster.errors import RasterError

# Web Mercator (EPSG:3857) uses spherical formulas with WGS84 semi-major axis.
WGS84_A = 6378137.0
EARTH_HALF = WGS84_A * math.pi
WEB_MERCATOR_MAX_LAT = math.degrees(math.atan(math.sinh(math.pi)))

# Chord-error stop for adaptive boundary transform (relative to dest-space edge length).
_CHORD_REL_TOL = 1e-12
_MAX_EDGE_DEPTH = 16


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


def _finite_pair(x: float, y: float) -> bool:
    return math.isfinite(x) and math.isfinite(y)


def _transform_point(transformer: Transformer, x: float, y: float) -> tuple[float, float]:
    tx, ty = transform_xy(transformer, np.array([x], dtype=np.float64), np.array([y], dtype=np.float64))
    return float(tx[0]), float(ty[0])


def _axis_aligned_corners(
    left: float,
    bottom: float,
    right: float,
    top: float,
) -> list[tuple[float, float]]:
    return [(left, bottom), (right, bottom), (right, top), (left, top)]


def _refine_edge(
    transformer: Transformer,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    depth: int,
) -> list[tuple[float, float]]:
    """Split an edge until the dest-space midpoint lies on the chord, up to a numeric bound."""
    t0 = _transform_point(transformer, x0, y0)
    t1 = _transform_point(transformer, x1, y1)
    mx = (x0 + x1) * 0.5
    my = (y0 + y1) * 0.5
    tm = _transform_point(transformer, mx, my)
    if not (_finite_pair(*t0) and _finite_pair(*t1) and _finite_pair(*tm)):
        return [t0, t1]
    chord_x = (t0[0] + t1[0]) * 0.5
    chord_y = (t0[1] + t1[1]) * 0.5
    dist = math.hypot(tm[0] - chord_x, tm[1] - chord_y)
    span = math.hypot(t1[0] - t0[0], t1[1] - t0[1])
    if depth >= _MAX_EDGE_DEPTH or dist <= _CHORD_REL_TOL * max(span, 1.0):
        return [t0, t1]
    left_pts = _refine_edge(transformer, x0, y0, mx, my, depth + 1)
    right_pts = _refine_edge(transformer, mx, my, x1, y1, depth + 1)
    return left_pts[:-1] + right_pts


def _aabb_from_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    finite = [(x, y) for x, y in points if _finite_pair(x, y)]
    if not finite:
        raise RasterError("Failed to transform raster bounds between CRS")
    xs = [p[0] for p in finite]
    ys = [p[1] for p in finite]
    return min(xs), min(ys), max(xs), max(ys)


def _mercator_pair(source: CRS, target: CRS) -> bool:
    a, b = crs_epsg(source), crs_epsg(target)
    return a in {4326, 3857} and b in {4326, 3857} and a != b


def transform_bounds(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Return (left, bottom, right, top) in ``target`` CRS.

    Axis-aligned rectangles in EPSG:4326 or EPSG:3857 map to each other with
    extrema at the four corners (x is linear in longitude, y is monotonic in
    latitude). Other CRS pairs refine each edge until the dest-space chord error
    is within floating-point tolerance.
    """
    left, bottom, right, top = bounds
    if crs_equal(source, target):
        return left, bottom, right, top
    transformer = make_transformer(source, target)
    corners = _axis_aligned_corners(left, bottom, right, top)
    if _mercator_pair(source, target):
        points = [_transform_point(transformer, x, y) for x, y in corners]
        return _aabb_from_points(points)

    points: list[tuple[float, float]] = []
    closed = corners + [corners[0]]
    for (x0, y0), (x1, y1) in zip(closed, closed[1:]):
        edge = _refine_edge(transformer, x0, y0, x1, y1, 0)
        if points:
            points.extend(edge[1:])
        else:
            points.extend(edge)
    return _aabb_from_points(points)


def transform_ring(
    source: CRS,
    target: CRS,
    bounds: tuple[float, float, float, float],
) -> list[list[float]]:
    """Closed boundary ring in ``target`` CRS."""
    left, bottom, right, top = bounds
    if crs_equal(source, target):
        ring = [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]
        return ring
    transformer = make_transformer(source, target)
    corners = _axis_aligned_corners(left, bottom, right, top)
    closed = corners + [corners[0]]
    if _mercator_pair(source, target):
        points = [_transform_point(transformer, x, y) for x, y in closed]
    else:
        points = []
        for (x0, y0), (x1, y1) in zip(closed, closed[1:]):
            edge = _refine_edge(transformer, x0, y0, x1, y1, 0)
            if points:
                points.extend(edge[1:])
            else:
                points.extend(edge)
    ring = [[x, y] for x, y in points if _finite_pair(x, y)]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def clip_mercator_lat(lat: float) -> float:
    return min(max(lat, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)


def destination_pixel_size(
    src_crs: CRS,
    dst_crs: CRS,
    src_affine: Affine,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Dest-CRS size of one source pixel, taking the finest of the four corners.

    An affine maps a rectangle to a parallelogram, so latitude/longitude extrema
    (and typically GSD extrema under common map projections) occur at vertices.
    Using the minimum avoids undersampling; it is not a center-pixel guess.
    """
    corners = (
        (0.0, 0.0),
        (float(width), 0.0),
        (float(width), float(height)),
        (0.0, float(height)),
    )
    dxs: list[float] = []
    dys: list[float] = []
    transformer = None if crs_equal(src_crs, dst_crs) else make_transformer(src_crs, dst_crs)
    for col, row in corners:
        x0, y0 = src_affine.xy(col, row)
        x1, y1 = src_affine.xy(col + 1.0, row)
        x2, y2 = src_affine.xy(col, row + 1.0)
        xs = np.array([x0, x1, x2], dtype=np.float64)
        ys = np.array([y0, y1, y2], dtype=np.float64)
        if transformer is None:
            tx, ty = xs, ys
        else:
            tx, ty = transform_xy(transformer, xs, ys)
        if not np.all(np.isfinite(tx) & np.isfinite(ty)):
            continue
        dxs.append(math.hypot(float(tx[1] - tx[0]), float(ty[1] - ty[0])))
        dys.append(math.hypot(float(tx[2] - tx[0]), float(ty[2] - ty[0])))
    if not dxs or not dys:
        raise RasterError("Failed to measure destination pixel size")
    return min(dxs), min(dys)


def grid_dimension(span: float, pixel_size: float) -> int:
    """Pixel count covering ``span`` at ``pixel_size`` without undersampling."""
    if span <= 0 or pixel_size <= 0:
        raise RasterError("Destination span and pixel size must be positive")
    return max(1, math.ceil(span / pixel_size - 1e-12))


def wgs84_bounds_from_rect(
    crs: CRS,
    bounds: tuple[float, float, float, float],
) -> list[float]:
    left, bottom, right, top = transform_bounds(crs, parse_crs("EPSG:4326"), bounds)
    west = min(max(left, -180.0), 180.0)
    east = min(max(right, -180.0), 180.0)
    south = min(max(bottom, -90.0), 90.0)
    north = min(max(top, -90.0), 90.0)
    return [west, south, east, north]
