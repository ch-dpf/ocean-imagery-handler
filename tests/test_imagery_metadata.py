"""imagery.json generation tests."""

import json
from pathlib import Path

import pytest

from app.schemas import TileFormat, TileProfile
from app.services.imagery_metadata import (
    ImageryMetadataError,
    build_imagery_json,
    ensure_imagery_json,
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


def test_build_imagery_json_mercator(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    bounds = [116.0, 39.0, 117.0, 40.0]

    meta = build_imagery_json(
        tiles_dir,
        TileProfile.MERCATOR,
        TileFormat.PNG,
        bounds,
        "http://localhost:8102/imagery",
        "test-set",
    )
    assert meta["tilingScheme"] == "web-mercator"
    assert meta["projection"] == "EPSG:3857"
    assert "test-set" in meta["urlTemplate"]
    assert meta["cesium"]["tilingSchemeClass"] == "WebMercatorTilingScheme"


def test_build_imagery_json_empty_raises(tmp_path: Path):
    with pytest.raises(ImageryMetadataError):
        build_imagery_json(
            tmp_path / "empty",
            TileProfile.MERCATOR,
            TileFormat.PNG,
            [0, 0, 1, 1],
            "http://localhost:8102/imagery",
            "empty",
        )


def test_ensure_imagery_json_writes_file(tmp_path: Path):
    tiles_dir = tmp_path / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)

    meta_path = ensure_imagery_json(
        tiles_dir,
        TileProfile.MERCATOR,
        TileFormat.PNG,
        [116.0, 39.0, 117.0, 40.0],
        "http://localhost:8102/imagery",
        "job-1",
    )
    assert meta_path.is_file()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["maximumLevel"] == 0
