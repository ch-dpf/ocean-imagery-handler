"""Synthetic GeoTIFF helpers for raster engine tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.geotiff import write_geotiff_array


def write_rgb_geotiff_4326(
    path: Path,
    *,
    width: int = 64,
    height: int = 64,
    west: float = 116.0,
    north: float = 40.0,
    pixel_deg: float = 0.001,
    color: tuple[int, int, int] = (20, 180, 80),
) -> Path:
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[:, :] = color
    # Distinct corners so resampling/warp is observable.
    data[0, 0] = (255, 0, 0)
    data[0, -1] = (0, 255, 0)
    data[-1, 0] = (0, 0, 255)
    data[-1, -1] = (255, 255, 0)
    affine = Affine.north_up(west, north, pixel_deg, pixel_deg)
    write_geotiff_array(
        path,
        data,
        affine=affine,
        crs=CRS.from_epsg(4326),
        compress="DEFLATE",
        block_size=32,
    )
    return path
