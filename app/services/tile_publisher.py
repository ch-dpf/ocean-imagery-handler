"""Register completed imagery tilesets for nginx static serving."""

import json
import logging
import os
import shutil
from pathlib import Path

from app.schemas import TileFormat, TileProfile, TileScheme
from app.services.tile_json import TILE_JSON, TileJsonError, _bounds_valid_wgs84, ensure_tile_json

logger = logging.getLogger(__name__)

_SWAGGER_PLACEHOLDERS = frozenset({"string", "example", "uuid", "name"})
_DEFAULT_BOUNDS = [-180.0, -90.0, 180.0, 90.0]
_EXT_TO_FORMAT = {
    "png": TileFormat.PNG,
    "jpg": TileFormat.JPEG,
    "jpeg": TileFormat.JPEG,
    "webp": TileFormat.WEBP,
}


class PublishError(RuntimeError):
    pass


def _resolve_tileset_name(job_id: str, tileset_name: str | None) -> str:
    if tileset_name is not None:
        candidate = tileset_name.strip()
        if candidate and candidate.lower() not in _SWAGGER_PLACEHOLDERS:
            name = candidate
        else:
            name = job_id
    else:
        name = job_id

    if not name:
        raise PublishError("tileset_name must not be empty")
    if "/" in name or "\\" in name or ".." in name:
        raise PublishError(f"Invalid tileset_name: {name}")
    return name


def resolve_job_tiles_dir(jobs_dir: Path, job_id: str) -> Path:
    """Return jobs/{job_id}/tiles, raising PublishError if missing."""
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise PublishError(f"Invalid job_id: {job_id}")
    tiles_dir = (jobs_dir / job_id / "tiles").resolve()
    if not _is_under_root(tiles_dir, jobs_dir):
        raise PublishError(f"Invalid job_id: {job_id}")
    if not tiles_dir.is_dir():
        raise PublishError(f"Tiles directory not found for job: {job_id}")
    return tiles_dir


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_tiles_dir_path(
    raw_path: str,
    *,
    workspace_dir: Path,
    jobs_dir: Path,
) -> Path:
    """Resolve a tiles directory path, restricting to workspace/jobs."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_dir / candidate
    tiles_dir = candidate.resolve()
    if not tiles_dir.is_dir():
        raise PublishError(f"Tiles directory not found: {raw_path}")
    if not (
        _is_under_root(tiles_dir, workspace_dir)
        or _is_under_root(tiles_dir, jobs_dir)
    ):
        raise PublishError("tiles_dir must be under the workspace")
    return tiles_dir


def _format_from_tile_url(tile_url: str | None) -> TileFormat | None:
    if not tile_url or "." not in tile_url:
        return None
    ext = tile_url.rsplit(".", 1)[-1].lower().split("?")[0]
    return _EXT_TO_FORMAT.get(ext)


def read_tile_json_publish_hints(tiles_dir: Path) -> dict:
    """Load publish parameters from an existing tile.json if present."""
    metadata_path = tiles_dir / TILE_JSON
    if not metadata_path.is_file():
        return {}

    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    hints: dict = {}
    profile = data.get("profile")
    if isinstance(profile, str):
        try:
            hints["profile"] = TileProfile(profile)
        except ValueError:
            pass

    scheme = data.get("scheme")
    if isinstance(scheme, str):
        try:
            hints["tile_scheme"] = TileScheme(scheme)
        except ValueError:
            pass

    bounds = data.get("bounds")
    if isinstance(bounds, list) and _bounds_valid_wgs84(bounds):
        hints["bounds_wgs84"] = [float(v) for v in bounds]

    tiles = data.get("tiles") or []
    fmt = _format_from_tile_url(tiles[0] if tiles else None)
    if fmt is not None:
        hints["tile_format"] = fmt

    return hints


def infer_job_id_from_tiles_dir(tiles_dir: Path, jobs_dir: Path) -> str | None:
    """If tiles_dir is jobs/{job_id}/tiles, return job_id."""
    try:
        relative = tiles_dir.resolve().relative_to(jobs_dir.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 2 and parts[-1] == "tiles":
        return parts[0]
    return None


def _register_tileset_link(tilesets_dir: Path, name: str, tiles_dir: Path) -> Path:
    """Expose tiles_dir under tilesets_dir/name via symlink."""
    tilesets_dir.mkdir(parents=True, exist_ok=True)
    link_path = tilesets_dir / name

    if link_path.is_symlink() or link_path.exists():
        if link_path.is_symlink():
            link_path.unlink()
        elif link_path.is_dir():
            shutil.rmtree(link_path)
        else:
            link_path.unlink()

    tiles_dir = tiles_dir.resolve()
    relative_target = os.path.relpath(tiles_dir, tilesets_dir.resolve())

    try:
        link_path.symlink_to(relative_target, target_is_directory=True)
        logger.info("Registered tileset symlink %s -> %s", link_path, relative_target)
        return link_path
    except OSError as exc:
        raise PublishError(
            f"Failed to create symlink for tileset '{name}': {exc}. "
            "Ensure the worker has permission to create symlinks."
        ) from exc


def publish_tileset(
    job_id: str,
    tiles_dir: Path,
    tilesets_dir: Path,
    public_url: str,
    base_path: str,
    profile: TileProfile,
    tile_format: TileFormat,
    bounds_wgs84: list[float],
    tileset_name: str | None = None,
    tile_scheme: TileScheme = TileScheme.XYZ,
) -> tuple[str, str, str]:
    """
    Prepare tile.json (TileJSON 3.0) and register tiles for nginx imagery-server.

    Returns (imagery_url, resolved_tileset_name, tile_url_template).
    """
    if not tiles_dir.is_dir():
        raise PublishError(f"Tiles directory not found: {tiles_dir}")

    name = _resolve_tileset_name(job_id, tileset_name)

    base = public_url.rstrip("/")
    path = base_path.rstrip("/")
    imagery_base_url = f"{base}{path}"

    try:
        metadata = ensure_tile_json(
            tiles_dir,
            profile,
            tile_format,
            bounds_wgs84,
            imagery_base_url,
            name,
            tile_scheme,
        )
    except TileJsonError as exc:
        raise PublishError(str(exc)) from exc

    metadata_data = json.loads(metadata.read_text(encoding="utf-8"))
    tiles = metadata_data.get("tiles") or []
    url_template = tiles[0] if tiles else ""

    _register_tileset_link(tilesets_dir, name, tiles_dir)

    imagery_url = f"{imagery_base_url}/{name}"
    return imagery_url, name, url_template


def publish_from_disk(
    *,
    jobs_dir: Path,
    workspace_dir: Path,
    tilesets_dir: Path,
    public_url: str,
    base_path: str,
    job_id: str | None = None,
    tiles_dir: str | Path | None = None,
    tileset_name: str | None = None,
    profile: TileProfile | None = None,
    tile_format: TileFormat | None = None,
    tile_scheme: TileScheme | None = None,
    bounds_wgs84: list[float] | None = None,
    gdal_cachemax: int | None = None,
) -> tuple[str, str, str, Path]:
    """
    Publish tiles from disk without requiring Redis job metadata.

    Prefer existing tile.json hints; optional request fields override them.
    Returns (imagery_url, tileset_name, url_template, resolved_tiles_dir).
    """
    if job_id and tiles_dir:
        raise PublishError("Provide either job_id or tiles_dir, not both")
    if not job_id and not tiles_dir:
        raise PublishError("Either job_id or tiles_dir is required")

    if job_id:
        resolved_tiles = resolve_job_tiles_dir(jobs_dir, job_id)
        resolved_job_id = job_id
    else:
        resolved_tiles = resolve_tiles_dir_path(
            str(tiles_dir),
            workspace_dir=workspace_dir,
            jobs_dir=jobs_dir,
        )
        resolved_job_id = infer_job_id_from_tiles_dir(resolved_tiles, jobs_dir) or (
            tileset_name.strip() if tileset_name and tileset_name.strip() else None
        )
        if not resolved_job_id:
            raise PublishError(
                "tileset_name is required when tiles_dir is not under jobs/{job_id}/tiles"
            )

    hints = read_tile_json_publish_hints(resolved_tiles)

    resolved_profile = profile or hints.get("profile") or TileProfile.MERCATOR
    resolved_format = tile_format or hints.get("tile_format") or TileFormat.PNG
    resolved_scheme = tile_scheme or hints.get("tile_scheme") or TileScheme.XYZ
    resolved_bounds = bounds_wgs84 or hints.get("bounds_wgs84")

    if resolved_bounds is None:
        job_dir = jobs_dir / resolved_job_id
        preprocessed = job_dir / "preprocess" / "preprocessed.tif"
        if preprocessed.is_file():
            from app.services.preprocessor import parse_wgs84_bounds

            env = {"GDAL_CACHEMAX": str(gdal_cachemax)} if gdal_cachemax is not None else None
            try:
                resolved_bounds = parse_wgs84_bounds(preprocessed, env=env)
            except Exception as exc:  # noqa: BLE001 - fall back to world bounds
                logger.warning("Failed to parse bounds from %s: %s", preprocessed, exc)
        if resolved_bounds is None:
            resolved_bounds = list(_DEFAULT_BOUNDS)
            logger.warning(
                "Publishing %s with default world bounds; provide bounds_wgs84 if needed",
                resolved_job_id,
            )

    imagery_url, name, url_template = publish_tileset(
        job_id=resolved_job_id,
        tiles_dir=resolved_tiles,
        tilesets_dir=tilesets_dir,
        public_url=public_url,
        base_path=base_path,
        profile=resolved_profile,
        tile_format=resolved_format,
        bounds_wgs84=resolved_bounds,
        tileset_name=tileset_name,
        tile_scheme=resolved_scheme,
    )
    return imagery_url, name, url_template, resolved_tiles


def unpublish_tileset(tilesets_dir: Path, tileset_name: str) -> None:
    """Remove a registered tileset symlink."""
    name = _resolve_tileset_name(tileset_name, tileset_name)
    link_path = tilesets_dir / name

    if not link_path.exists() and not link_path.is_symlink():
        raise PublishError(f"Tileset not published: {name}")

    if link_path.is_symlink():
        link_path.unlink()
    elif link_path.is_dir():
        shutil.rmtree(link_path)
    else:
        link_path.unlink()

    logger.info("Unpublished tileset %s", name)


def list_published_tilesets(tilesets_dir: Path) -> list[str]:
    """List tileset names registered under tilesets_dir."""
    if not tilesets_dir.is_dir():
        return []

    names: list[str] = []
    for entry in sorted(tilesets_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() or entry.is_symlink():
            names.append(entry.name)
    return names
