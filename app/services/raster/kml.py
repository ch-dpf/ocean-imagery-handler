"""Simple KML document for a generated tileset."""

from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

from app.services.raster.crsutil import EARTH_HALF, WEB_MERCATOR_MAX_LAT


def _mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = (x / EARTH_HALF) * 180.0
    lat_rad = 2.0 * math.atan(math.exp(y / EARTH_HALF * math.pi)) - math.pi / 2.0
    lat = max(min(math.degrees(lat_rad), WEB_MERCATOR_MAX_LAT), -WEB_MERCATOR_MAX_LAT)
    return lon, lat


def mercator_tile_latlon_box(z: int, x: int, y_xyz: int) -> tuple[float, float, float, float]:
    n = 2 ** z
    size = (2 * EARTH_HALF) / n
    minx = -EARTH_HALF + x * size
    maxx = -EARTH_HALF + (x + 1) * size
    maxy = EARTH_HALF - y_xyz * size
    miny = EARTH_HALF - (y_xyz + 1) * size
    west, north = _mercator_to_lonlat(minx, maxy)
    east, south = _mercator_to_lonlat(maxx, miny)
    return west, south, east, north


def geodetic_tile_latlon_box(z: int, x: int, y_xyz: int) -> tuple[float, float, float, float]:
    n_x = 2 ** (z + 1)
    n_y = 2 ** z
    west = -180.0 + x * 360.0 / n_x
    east = -180.0 + (x + 1) * 360.0 / n_x
    north = 90.0 - y_xyz * 180.0 / n_y
    south = 90.0 - (y_xyz + 1) * 180.0 / n_y
    return west, south, east, north


def write_doc_kml(
    output_dir: Path,
    tiles: list[tuple[int, int, int]],
    *,
    ext: str,
    profile: str,
    scheme: str,
) -> None:
    """Write a lightweight GroundOverlay KML for the listed XYZ tiles."""
    overlays: list[str] = []
    for z, x, y in tiles:
        file_y = y if scheme == "xyz" else (2**z - 1 - y)
        href = f"{z}/{x}/{file_y}.{ext}"
        if profile == "geodetic":
            west, south, east, north = geodetic_tile_latlon_box(z, x, y)
        else:
            west, south, east, north = mercator_tile_latlon_box(z, x, y)
        overlays.append(
            "\n".join(
                [
                    "    <GroundOverlay>",
                    f"      <name>{z}/{x}/{file_y}</name>",
                    "      <Icon>",
                    f"        <href>{escape(href)}</href>",
                    "      </Icon>",
                    "      <LatLonBox>",
                    f"        <north>{north}</north>",
                    f"        <south>{south}</south>",
                    f"        <east>{east}</east>",
                    f"        <west>{west}</west>",
                    "      </LatLonBox>",
                    "    </GroundOverlay>",
                ]
            )
        )
    body = "\n".join(overlays)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "  <Document>\n"
        "    <name>tiles</name>\n"
        f"{body}\n"
        "  </Document>\n"
        "</kml>\n"
    )
    (output_dir / "doc.kml").write_text(xml, encoding="utf-8")
