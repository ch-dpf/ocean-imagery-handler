"""tile.json (TileJSON 3.0) generation tests."""

import json
from pathlib import Path

import pytest

from app.schemas import TileFormat, TileProfile, TileScheme
from app.services.tile_json import (
    TileJsonError,
    build_tile_json,
    ensure_tile_json,
    scan_tile_extents,
)


def _make_tile(tiles_dir: Path, z: int, x: int, y: int, ext: str = "png") -> None:
    tile_path = tiles_dir / str(z) / str(x)
    tile_path.mkdir(parents=True, exist_ok=True)
    (tile_path / f"{y}.{ext}").write_bytes(b"\x00")


def test_scan_tile_extents_with_tiles(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    _make_tile(tiles_dir, 0, 1, 0)
    _make_tile(tiles_dir, 1, 2, 4)

    levels = scan_tile_extents(tiles_dir)
    assert levels[0] == (0, 0, 1, 0)
    assert levels[1] == (2, 4, 2, 4)


def test_build_tile_json_mercator(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    bounds = [116.0, 39.0, 117.0, 40.0]

    meta = build_tile_json(
        tiles_dir,
        TileProfile.MERCATOR,
        TileFormat.PNG,
        bounds,
        "http://localhost:8102/imagery",
        "test-set",
    )
    assert meta["tilejson"] == "3.0.0"
    assert meta["scheme"] == "xyz"
    assert meta["minzoom"] == 0
    assert meta["maxzoom"] == 0
    assert meta["bounds"] == bounds
    assert meta["center"] == [116.5, 39.5, 0]
    assert len(meta["tiles"]) == 1
    assert "test-set" in meta["tiles"][0]
    assert meta["tiles"][0].endswith("/{z}/{x}/{y}.png")
    assert "urlTemplate" not in meta
    assert "cesium" not in meta


def test_build_tile_json_tms(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    bounds = [116.0, 39.0, 117.0, 40.0]

    meta = build_tile_json(
        tiles_dir,
        TileProfile.MERCATOR,
        TileFormat.PNG,
        bounds,
        "http://localhost:8102/imagery",
        "test-set",
        tile_scheme=TileScheme.TMS,
    )
    assert meta["scheme"] == "tms"
    assert "{y}" in meta["tiles"][0]
    assert "{reverseY}" not in meta["tiles"][0]


def test_build_tile_json_empty_raises(tmp_path: Path):
    with pytest.raises(TileJsonError):
        build_tile_json(
            tmp_path / "empty",
            TileProfile.MERCATOR,
            TileFormat.PNG,
            [0, 0, 1, 1],
            "http://localhost:8102/imagery",
            "empty",
        )


def test_ensure_tile_json_writes_file(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    legacy = tiles_dir / "imagery.json"
    legacy.write_text("{}", encoding="utf-8")

    meta_path = ensure_tile_json(
        tiles_dir,
        TileProfile.MERCATOR,
        TileFormat.PNG,
        [116.0, 39.0, 117.0, 40.0],
        "http://localhost:8102/imagery",
        "job-1",
    )
    assert meta_path.is_file()
    assert meta_path.name == "tile.json"
    assert not legacy.exists()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["maxzoom"] == 0
    assert data["tilejson"] == "3.0.0"
