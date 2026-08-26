#!/usr/bin/env python3
"""Fix tile.json bounds that were stored as EPSG:3857 metres."""

import json
import subprocess
import sys
from pathlib import Path


def wgs84_bounds(dataset: Path) -> list[float]:
    result = subprocess.run(
        ["gdalinfo", "-json", str(dataset)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    data = json.loads(result.stdout)
    extent = data.get("wgs84Extent") or {}
    ring = (extent.get("coordinates") or [[]])[0]
    if not ring:
        raise RuntimeError(f"No wgs84Extent for {dataset}")

    lons = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    return [min(lons), min(lats), max(lons), max(lats)]


def fix_tile_json(tile_json: Path, bounds: list[float]) -> None:
    data = json.loads(tile_json.read_text(encoding="utf-8"))
    data["bounds"] = bounds
    if isinstance(data.get("center"), list) and len(data["center"]) >= 2:
        west, south, east, north = bounds
        zoom = data["center"][2] if len(data["center"]) > 2 else 0
        data["center"] = [(west + east) / 2.0, (south + north) / 2.0, zoom]
    tile_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Updated {tile_json} -> {bounds}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: fix_bounds.py <job_id> [<job_id>...]")
        return 1

    workspace = Path("/data/workspace")
    for job_id in sys.argv[1:]:
        job_dir = workspace / "jobs" / job_id
        dataset = job_dir / "preprocess" / "preprocessed.tif"
        tile_json = job_dir / "tiles" / "tile.json"
        if not dataset.is_file():
            print(f"Skip {job_id}: missing {dataset}")
            continue
        if not tile_json.is_file():
            print(f"Skip {job_id}: missing {tile_json}")
            continue
        bounds = wgs84_bounds(dataset)
        fix_tile_json(tile_json, bounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
