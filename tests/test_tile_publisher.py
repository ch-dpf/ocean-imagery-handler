"""Tile publishing tests."""

import os
from pathlib import Path

import pytest

from app.schemas import TileFormat, TileProfile
from app.services.tile_publisher import (
    PublishError,
    display_meta_path,
    get_tileset_display_meta,
    list_published_tilesets,
    publish_tileset,
    unpublish_tileset,
    write_tileset_display_meta,
)


def _make_tile(tiles_dir: Path, z: int, x: int, y: int) -> None:
    tile_path = tiles_dir / str(z) / str(x)
    tile_path.mkdir(parents=True, exist_ok=True)
    (tile_path / f"{y}.png").write_bytes(b"\x00")


@pytest.fixture
def tile_dirs(tmp_path: Path):
    tiles_dir = tmp_path / "jobs" / "job-1" / "tiles"
    tilesets_dir = tmp_path / "tilesets" / "imagery"
    _make_tile(tiles_dir, 0, 0, 0)
    bounds = [116.0, 39.0, 117.0, 40.0]
    return tiles_dir, tilesets_dir, bounds


def test_publish_tileset_creates_symlink_and_tile_json(tile_dirs):
    tiles_dir, tilesets_dir, bounds = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    imagery_url, name, url_template = publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8102",
        base_path="/imagery",
        profile=TileProfile.MERCATOR,
        tile_format=TileFormat.PNG,
        bounds_wgs84=bounds,
    )

    assert name == "job-1"
    assert imagery_url == "http://localhost:8102/imagery/job-1"
    assert "{z}" in url_template
    assert (tilesets_dir / "job-1").is_symlink()
    assert (tiles_dir / "tile.json").is_file()
    assert display_meta_path(tilesets_dir, "job-1").is_file()
    assert list_published_tilesets(tilesets_dir) == ["job-1"]


def test_publish_tileset_rejects_invalid_name(tile_dirs):
    tiles_dir, tilesets_dir, bounds = tile_dirs
    with pytest.raises(PublishError):
        publish_tileset(
            job_id="job-1",
            tiles_dir=tiles_dir,
            tilesets_dir=tilesets_dir,
            public_url="http://localhost:8102",
            base_path="/imagery",
            profile=TileProfile.MERCATOR,
            tile_format=TileFormat.PNG,
            bounds_wgs84=bounds,
            tileset_name="../evil",
        )


def test_unpublish_tileset(tile_dirs):
    tiles_dir, tilesets_dir, bounds = tile_dirs
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    publish_tileset(
        job_id="job-1",
        tiles_dir=tiles_dir,
        tilesets_dir=tilesets_dir,
        public_url="http://localhost:8102",
        base_path="/imagery",
        profile=TileProfile.MERCATOR,
        tile_format=TileFormat.PNG,
        bounds_wgs84=bounds,
    )

    unpublish_tileset(tilesets_dir, "job-1")
    assert list_published_tilesets(tilesets_dir) == []
    assert not display_meta_path(tilesets_dir, "job-1").is_file()


def test_list_published_tilesets_symlink_first(tmp_path: Path):
    tilesets_dir = tmp_path / "tilesets"
    tilesets_dir.mkdir()
    (tilesets_dir / ".hidden-meta.layer-meta.json").write_text("{}", encoding="utf-8")
    (tilesets_dir / "plain.txt").write_text("x", encoding="utf-8")
    real_dir = tilesets_dir / "copied-set"
    real_dir.mkdir()
    if os.name == "nt":
        # Still list real directories even when symlink creation is unavailable.
        assert list_published_tilesets(tilesets_dir) == ["copied-set"]
        return

    target = tmp_path / "tiles"
    target.mkdir()
    (tilesets_dir / "linked-set").symlink_to(target, target_is_directory=True)
    assert list_published_tilesets(tilesets_dir) == ["copied-set", "linked-set"]


def test_get_tileset_display_meta_uses_sidecar_without_tiles(tmp_path: Path):
    from app.services.tile_publisher import _display_meta_memory

    tilesets_dir = tmp_path / "tilesets"
    tilesets_dir.mkdir()
    write_tileset_display_meta(
        tilesets_dir,
        "coast",
        {
            "url_template": "http://localhost/imagery/coast/{z}/{x}/{y}.png",
            "scheme": "xyz",
            "min_zoom": 0,
            "max_zoom": 12,
            "profile": "mercator",
            "crs": "EPSG:3857 (Web Mercator)",
            "bounds": [116.0, 39.0, 117.0, 40.0],
        },
    )
    _display_meta_memory.clear()

    meta = get_tileset_display_meta(tilesets_dir, "coast")
    assert meta["scheme"] == "xyz"
    assert meta["max_zoom"] == 12
    assert meta["bounds"] == [116.0, 39.0, 117.0, 40.0]

    # Second call hits memory cache.
    meta2 = get_tileset_display_meta(tilesets_dir, "coast")
    assert meta2["min_zoom"] == 0
