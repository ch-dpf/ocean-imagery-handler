"""Raster metadata (replacement for ``gdal raster info``)."""

from __future__ import annotations

from pathlib import Path

from pyproj import CRS

from app.services.raster.crsutil import densify_rect, make_transformer, transform_xy, wgs84_bounds_from_rect
from app.services.raster.geotiff import GeoTiffReader


def raster_info_json(dataset: Path, cache_bytes: int = 64 * 1024 * 1024) -> dict:
    with GeoTiffReader(dataset, cache_bytes=cache_bytes) as src:
        left, bottom, right, top = src.bounds
        ul = src.affine.xy(0, 0)
        ur = src.affine.xy(src.width, 0)
        lr = src.affine.xy(src.width, src.height)
        ll = src.affine.xy(0, src.height)
        cx, cy = src.affine.xy(src.width / 2.0, src.height / 2.0)
        west, south, east, north = wgs84_bounds_from_rect(src.crs, src.bounds)
        xs, ys = densify_rect(left, bottom, right, top)
        transformer = make_transformer(src.crs, CRS.from_epsg(4326))
        lon, lat = transform_xy(transformer, xs, ys)
        ring = [[float(x), float(y)] for x, y in zip(lon, lat) if np_isfinite(x) and np_isfinite(y)]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        return {
            "size": [src.width, src.height],
            "bands": [{"band": i + 1, "colorInterp": _color_interp(src.samples, i)} for i in range(src.samples)],
            "coordinateSystem": {"wkt": src.crs.to_wkt(), "epsg": src.crs.to_epsg()},
            "geoTransform": src.affine.to_gdal(),
            "cornerCoordinates": {
                "upperLeft": [float(ul[0]), float(ul[1])],
                "lowerLeft": [float(ll[0]), float(ll[1])],
                "lowerRight": [float(lr[0]), float(lr[1])],
                "upperRight": [float(ur[0]), float(ur[1])],
                "center": [float(cx), float(cy)],
            },
            "wgs84Extent": {"type": "Polygon", "coordinates": [ring] if ring else []},
            "wgs84Bounds": [west, south, east, north],
        }


def np_isfinite(value: object) -> bool:
    try:
        import math

        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _color_interp(samples: int, index: int) -> str:
    if samples in {3, 4}:
        return ("Red", "Green", "Blue", "Alpha")[index] if index < 4 else f"Band{index + 1}"
    if samples == 2:
        return ("Gray", "Alpha")[index]
    return "Gray" if index == 0 else f"Band{index + 1}"


def raster_info_text(dataset: Path, cache_bytes: int = 64 * 1024 * 1024) -> str:
    data = raster_info_json(dataset, cache_bytes=cache_bytes)
    size = data["size"]
    gt = data["geoTransform"]
    corners = data["cornerCoordinates"]
    epsg = data["coordinateSystem"].get("epsg")
    lines = [
        f"Size is {size[0]}, {size[1]}",
        f"Coordinate System is: EPSG:{epsg}" if epsg else "Coordinate System is:",
        f"Origin = ({gt[0]}, {gt[3]})",
        f"Pixel Size = ({gt[1]}, {gt[5]})",
        "Corner Coordinates:",
        f"Upper Left  ({corners['upperLeft'][0]:.6f}, {corners['upperLeft'][1]:.6f})",
        f"Lower Left  ({corners['lowerLeft'][0]:.6f}, {corners['lowerLeft'][1]:.6f})",
        f"Upper Right ({corners['upperRight'][0]:.6f}, {corners['upperRight'][1]:.6f})",
        f"Lower Right ({corners['lowerRight'][0]:.6f}, {corners['lowerRight'][1]:.6f})",
        f"Center      ({corners['center'][0]:.6f}, {corners['center'][1]:.6f})",
    ]
    bounds = data.get("wgs84Bounds")
    if bounds:
        lines.append(f"WGS84 Bounds = {bounds[0]:.6f}, {bounds[1]:.6f}, {bounds[2]:.6f}, {bounds[3]:.6f}")
    return "\n".join(lines) + "\n"


def wgs84_bounds(dataset: Path, cache_bytes: int = 64 * 1024 * 1024) -> list[float]:
    with GeoTiffReader(dataset, cache_bytes=cache_bytes) as src:
        return wgs84_bounds_from_rect(src.crs, src.bounds)
