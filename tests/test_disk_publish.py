"""Disk-based publish (no Redis) tests."""

import json
import os
from pathlib import Path

import pytest

from app.schemas import TileFormat, TileProfile, TileScheme
from app.services.tile_json import TILE_JSON
from app.services.tile_publisher import (
    PublishError,
    list_published_tilesets,
    publish_from_disk,
    read_tile_json_publish_hints,
    resolve_job_tiles_dir,
    unpublish_tileset,
)


def _make_tile(tiles_dir: Path, z: int, x: int, y: int) -> None:
    tile_path = tiles_dir / str(z) / str(x)
    tile_path.mkdir(parents=True, exist_ok=True)
    (tile_path / f"{y}.png").write_bytes(b"\x00")


def _write_tile_json(tiles_dir: Path, *, name: str = "job-expired") -> None:
    (tiles_dir / TILE_JSON).write_text(
        json.dumps(
            {
                "tilejson": "3.0.0",
                "name": name,
                "scheme": "xyz",
                "profile": "mercator",
                "tiles": [f"http://localhost:8102/imagery/{name}/{{z}}/{{x}}/{{y}}.png"],
                "minzoom": 0,
                "maxzoom": 0,
                "bounds": [116.0, 39.0, 117.0, 40.0],
                "center": [116.5, 39.5, 0],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path):
    jobs_dir = tmp_path / "jobs"
    tiles_dir = jobs_dir / "job-expired" / "tiles"
    _make_tile(tiles_dir, 0, 0, 0)
    _write_tile_json(tiles_dir)
    tilesets_dir = tmp_path / "tilesets" / "imagery"
    return {
        "workspace_dir": tmp_path,
        "jobs_dir": jobs_dir,
        "tiles_dir": tiles_dir,
        "tilesets_dir": tilesets_dir,
    }


def test_read_tile_json_publish_hints(workspace):
    hints = read_tile_json_publish_hints(workspace["tiles_dir"])
    assert hints["profile"] == TileProfile.MERCATOR
    assert hints["tile_scheme"] == TileScheme.XYZ
    assert hints["tile_format"] == TileFormat.PNG
    assert hints["bounds_wgs84"] == [116.0, 39.0, 117.0, 40.0]


def test_resolve_job_tiles_dir(workspace):
    resolved = resolve_job_tiles_dir(workspace["jobs_dir"], "job-expired")
    assert resolved == workspace["tiles_dir"].resolve()


def test_resolve_job_tiles_dir_missing(workspace):
    with pytest.raises(PublishError, match="not found"):
        resolve_job_tiles_dir(workspace["jobs_dir"], "missing-job")


def test_publish_from_disk_by_job_id(workspace):
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    imagery_url, name, url_template, tiles_dir = publish_from_disk(
        jobs_dir=workspace["jobs_dir"],
        workspace_dir=workspace["workspace_dir"],
        tilesets_dir=workspace["tilesets_dir"],
        public_url="http://localhost:8102",
        base_path="/imagery",
        job_id="job-expired",
    )

    assert name == "job-expired"
    assert imagery_url == "http://localhost:8102/imagery/job-expired"
    assert "{z}" in url_template
    assert tiles_dir == workspace["tiles_dir"].resolve()
    assert list_published_tilesets(workspace["tilesets_dir"]) == ["job-expired"]

    unpublish_tileset(workspace["tilesets_dir"], "job-expired")
    assert list_published_tilesets(workspace["tilesets_dir"]) == []


def test_publish_from_disk_by_tiles_dir(workspace):
    if os.name == "nt":
        pytest.skip("Symlink creation may require elevated privileges on Windows")

    imagery_url, name, _url, _tiles = publish_from_disk(
        jobs_dir=workspace["jobs_dir"],
        workspace_dir=workspace["workspace_dir"],
        tilesets_dir=workspace["tilesets_dir"],
        public_url="http://localhost:8102",
        base_path="/imagery",
        tiles_dir=str(workspace["tiles_dir"]),
        tileset_name="custom-name",
    )

    assert name == "custom-name"
    assert imagery_url.endswith("/imagery/custom-name")
    assert list_published_tilesets(workspace["tilesets_dir"]) == ["custom-name"]


def test_publish_from_disk_rejects_both_ids(workspace):
    with pytest.raises(PublishError, match="either job_id or tiles_dir"):
        publish_from_disk(
            jobs_dir=workspace["jobs_dir"],
            workspace_dir=workspace["workspace_dir"],
            tilesets_dir=workspace["tilesets_dir"],
            public_url="http://localhost:8102",
            base_path="/imagery",
            job_id="job-expired",
            tiles_dir=str(workspace["tiles_dir"]),
        )


def test_publish_from_disk_rejects_outside_workspace(tmp_path: Path):
    outside = tmp_path / "outside" / "tiles"
    _make_tile(outside, 0, 0, 0)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    jobs_dir = workspace_dir / "jobs"
    jobs_dir.mkdir()

    with pytest.raises(PublishError, match="under the workspace"):
        publish_from_disk(
            jobs_dir=jobs_dir,
            workspace_dir=workspace_dir,
            tilesets_dir=workspace_dir / "tilesets",
            public_url="http://localhost:8102",
            base_path="/imagery",
            tiles_dir=str(outside),
            tileset_name="evil",
        )
