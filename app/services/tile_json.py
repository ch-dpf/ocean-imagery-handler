"""Generate TileJSON 3.0 metadata (tile.json) for imagery tilesets."""

import json
import logging
from pathlib import Path
from typing import TypedDict

from app.schemas import TileFormat, TileProfile, TileScheme

logger = logging.getLogger(__name__)

TILE_JSON = "tile.json"
TILEJSON_VERSION = "3.0.0"

TILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class TilesetDisplayMeta(TypedDict):
    url_template: str | None
    scheme: str | None
    min_zoom: int | None
    max_zoom: int | None
    profile: str | None
    crs: str | None
    bounds: list[float] | None


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


class TileJsonError(RuntimeError):
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


def build_tile_json(
    tiles_dir: Path,
    profile: TileProfile,
    tile_format: TileFormat,
    bounds_wgs84: list[float],
    imagery_base_url: str,
    tileset_name: str,
    tile_scheme: TileScheme = TileScheme.XYZ,
) -> dict:
    """Build TileJSON 3.0 for an XYZ/TMS imagery tileset."""
    levels = scan_tile_extents(tiles_dir)
    if not levels:
        raise TileJsonError(f"No imagery tiles found under {tiles_dir}")

    min_zoom = min(levels)
    max_zoom = max(levels)
    ext = detect_tile_extension(tiles_dir)
    if ext == "jpeg":
        ext = "jpg"

    base = imagery_base_url.rstrip("/")
    # TileJSON always uses {y}; clients honor scheme (xyz|tms) for Y orientation.
    tile_url = f"{base}/{tileset_name}/{{z}}/{{x}}/{{y}}.{ext}"

    west, south, east, north = [float(v) for v in bounds_wgs84]
    center_zoom = (min_zoom + max_zoom) // 2
    _ = tile_format

    return {
        "tilejson": TILEJSON_VERSION,
        "name": tileset_name,
        "scheme": tile_scheme.value,
        "profile": profile.value,
        "tiles": [tile_url],
        "minzoom": min_zoom,
        "maxzoom": max_zoom,
        "bounds": [west, south, east, north],
        "center": [(west + east) / 2.0, (south + north) / 2.0, center_zoom],
    }


PROFILE_CRS_LABELS: dict[str, str] = {
    TileProfile.MERCATOR.value: "EPSG:3857 (Web Mercator)",
    TileProfile.GEODETIC.value: "EPSG:4326 (WGS84 / Geodetic)",
    TileProfile.RASTER.value: "本地像素 (Raster)",
}


def crs_label_for_profile(profile: str | None) -> str:
    if not profile:
        return PROFILE_CRS_LABELS[TileProfile.MERCATOR.value]
    return PROFILE_CRS_LABELS.get(profile, PROFILE_CRS_LABELS[TileProfile.MERCATOR.value])


def scheme_label(scheme: str | None) -> str:
    if scheme == TileScheme.TMS.value:
        return "TMS"
    if scheme == TileScheme.XYZ.value:
        return "XYZ"
    return scheme.upper() if scheme else "—"


def empty_tileset_display_meta() -> TilesetDisplayMeta:
    return {
        "url_template": None,
        "scheme": None,
        "min_zoom": None,
        "max_zoom": None,
        "profile": None,
        "crs": None,
        "bounds": None,
    }


def read_tile_display_metadata(tiles_dir: Path) -> TilesetDisplayMeta:
    """Read list/display fields from ``tile.json`` under a tiles directory.

    Missing or unreadable files yield None values without raising.
    """
    empty = empty_tileset_display_meta()
    metadata_path = tiles_dir / TILE_JSON
    if not metadata_path.is_file():
        return empty

    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty

    if not isinstance(data, dict):
        return empty

    tiles = data.get("tiles") or []
    url_template = tiles[0] if tiles and isinstance(tiles[0], str) else None
    scheme = data.get("scheme") if isinstance(data.get("scheme"), str) else None
    profile = data.get("profile") if isinstance(data.get("profile"), str) else None
    min_zoom = data.get("minzoom") if isinstance(data.get("minzoom"), int) else None
    max_zoom = data.get("maxzoom") if isinstance(data.get("maxzoom"), int) else None

    bounds = None
    raw_bounds = data.get("bounds")
    if isinstance(raw_bounds, list) and len(raw_bounds) == 4:
        try:
            bounds = [float(v) for v in raw_bounds]
        except (TypeError, ValueError):
            bounds = None

    return {
        "url_template": url_template,
        "scheme": scheme,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "profile": profile,
        "crs": crs_label_for_profile(profile),
        "bounds": bounds,
    }


def ensure_tile_json(
    tiles_dir: Path,
    profile: TileProfile,
    tile_format: TileFormat,
    bounds_wgs84: list[float],
    imagery_base_url: str,
    tileset_name: str,
    tile_scheme: TileScheme = TileScheme.XYZ,
) -> Path:
    """Ensure tile.json exists in tiles_dir; generate if missing or stale."""
    metadata_path = tiles_dir / TILE_JSON
    # Drop legacy Cesium-oriented metadata if present.
    legacy_path = tiles_dir / "imagery.json"
    if legacy_path.is_file():
        legacy_path.unlink(missing_ok=True)

    if metadata_path.is_file():
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected_prefix = f"{imagery_base_url.rstrip('/')}/{tileset_name}/"
            tiles = data.get("tiles") or []
            existing_template = tiles[0] if tiles else ""
            if (
                isinstance(existing_template, str)
                and existing_template.startswith(expected_prefix)
                and data.get("maxzoom") is not None
                and _bounds_valid_wgs84(data.get("bounds", []))
                and data.get("scheme", TileScheme.XYZ.value) == tile_scheme.value
                and data.get("tilejson") == TILEJSON_VERSION
            ):
                logger.info("Using existing tile.json at %s", metadata_path)
                return metadata_path
            if existing_template and not existing_template.startswith(expected_prefix):
                logger.warning(
                    "Regenerating tile.json at %s due to public URL change",
                    metadata_path,
                )
            elif tiles and data.get("maxzoom") is not None:
                logger.warning("Regenerating tile.json at %s due to invalid bounds", metadata_path)
        except (json.JSONDecodeError, OSError, IndexError, TypeError) as exc:
            logger.warning("Invalid tile.json at %s, regenerating: %s", metadata_path, exc)

    content = build_tile_json(
        tiles_dir,
        profile,
        tile_format,
        bounds_wgs84,
        imagery_base_url,
        tileset_name,
        tile_scheme,
    )
    metadata_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    logger.info("Wrote tile.json to %s", metadata_path)
    return metadata_path
