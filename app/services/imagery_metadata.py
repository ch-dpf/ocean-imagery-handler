"""Generate imagery.json metadata for Cesium clients."""

import json
import logging
from pathlib import Path

from app.schemas import TileFormat, TileProfile, TileScheme

logger = logging.getLogger(__name__)

IMAGERY_JSON = "imagery.json"

TILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _bounds_valid_wgs84(bounds: list[float] | list) -> bool:
    try:
        west, south, east, north = [float(value) for value in bounds]
    except (TypeError, ValueError):
        return False

    return (
        -180.0 <= west <= 180.0
        and -180.0 <= east <= 180.0
        and -90.0 <= south <= 90.0
        and -90.0 <= north <= 90.0
        and west < east
        and south < north
    )


class ImageryMetadataError(RuntimeError):
    pass


def scan_tile_extents(tiles_dir: Path) -> dict[int, tuple[int, int, int, int]]:
    """Scan {z}/{x}/{y}.png layout and return zoom -> (minX, minY, maxX, maxY)."""
    levels: dict[int, tuple[int, int, int, int]] = {}

    if not tiles_dir.is_dir():
        return levels

    for z_path in sorted(tiles_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not z_path.is_dir() or not z_path.name.isdigit():
            continue

        zoom = int(z_path.name)
        xs: list[int] = []
        ys: list[int] = []

        for x_path in z_path.iterdir():
            if not x_path.is_dir() or not x_path.name.isdigit():
                continue
            x = int(x_path.name)
            for tile_file in x_path.iterdir():
                if tile_file.suffix.lower() in TILE_EXTENSIONS and tile_file.stem.isdigit():
                    xs.append(x)
                    ys.append(int(tile_file.stem))

        if xs and ys:
            levels[zoom] = (min(xs), min(ys), max(xs), max(ys))

    return levels


def detect_tile_extension(tiles_dir: Path) -> str:
    for z_path in tiles_dir.iterdir():
        if not z_path.is_dir() or not z_path.name.isdigit():
            continue
        for x_path in z_path.iterdir():
            if not x_path.is_dir():
                continue
            for tile_file in x_path.iterdir():
                if tile_file.suffix.lower() in TILE_EXTENSIONS:
                    return tile_file.suffix.lower().lstrip(".")
    return "png"


def build_imagery_json(
    tiles_dir: Path,
    profile: TileProfile,
    tile_format: TileFormat,
    bounds_wgs84: list[float],
    imagery_base_url: str,
    tileset_name: str,
    tile_scheme: TileScheme = TileScheme.XYZ,
) -> dict:
    """Build imagery.json for Cesium UrlTemplateImageryProvider."""
    levels = scan_tile_extents(tiles_dir)
    if not levels:
        raise ImageryMetadataError(f"No imagery tiles found under {tiles_dir}")

    min_zoom = min(levels)
    max_zoom = max(levels)
    ext = detect_tile_extension(tiles_dir)
    if ext == "jpeg":
        ext = "jpg"

    base = imagery_base_url.rstrip("/")
    y_placeholder = "{reverseY}" if tile_scheme == TileScheme.TMS else "{y}"
    url_template = f"{base}/{tileset_name}/{{z}}/{{x}}/{y_placeholder}.{ext}"

    tiling_scheme = "web-mercator" if profile == TileProfile.MERCATOR else "geographic"
    flip_y = tile_scheme == TileScheme.TMS

    return {
        "name": tileset_name,
        "format": tile_format.value,
        "tilingScheme": tiling_scheme,
        "tileScheme": tile_scheme.value,
        "projection": "EPSG:3857" if profile == TileProfile.MERCATOR else "EPSG:4326",
        "bounds": bounds_wgs84,
        "minimumLevel": min_zoom,
        "maximumLevel": max_zoom,
        "tileWidth": 256,
        "tileHeight": 256,
        "urlTemplate": url_template,
        "flipY": flip_y,
        "levels": {
            str(z): {"minX": v[0], "minY": v[1], "maxX": v[2], "maxY": v[3]}
            for z, v in levels.items()
        },
        "cesium": {
            "tilingSchemeClass": (
                "WebMercatorTilingScheme" if profile == TileProfile.MERCATOR else "GeographicTilingScheme"
            ),
            "urlTemplate": url_template,
            "minimumLevel": min_zoom,
            "maximumLevel": max_zoom,
            "rectangle": bounds_wgs84,
            "flipY": flip_y,
        },
    }


def ensure_imagery_json(
    tiles_dir: Path,
    profile: TileProfile,
    tile_format: TileFormat,
    bounds_wgs84: list[float],
    imagery_base_url: str,
    tileset_name: str,
    tile_scheme: TileScheme = TileScheme.XYZ,
) -> Path:
    """Ensure imagery.json exists in tiles_dir; generate if missing."""
    metadata_path = tiles_dir / IMAGERY_JSON

    if metadata_path.is_file():
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected_prefix = f"{imagery_base_url.rstrip('/')}/{tileset_name}/"
            existing_template = data.get("urlTemplate") or ""
            if (
                existing_template.startswith(expected_prefix)
                and data.get("maximumLevel") is not None
                and _bounds_valid_wgs84(data.get("bounds", []))
                and data.get("tileScheme", TileScheme.XYZ.value) == tile_scheme.value
            ):
                logger.info("Using existing imagery.json at %s", metadata_path)
                return metadata_path
            if existing_template and not existing_template.startswith(expected_prefix):
                logger.warning(
                    "Regenerating imagery.json at %s due to public URL change",
                    metadata_path,
                )
            elif data.get("urlTemplate") and data.get("maximumLevel") is not None:
                logger.warning("Regenerating imagery.json at %s due to invalid bounds", metadata_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Invalid imagery.json at %s, regenerating: %s", metadata_path, exc)

    content = build_imagery_json(
        tiles_dir,
        profile,
        tile_format,
        bounds_wgs84,
        imagery_base_url,
        tileset_name,
        tile_scheme,
    )
    metadata_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    logger.info("Wrote imagery.json to %s", metadata_path)
    return metadata_path
