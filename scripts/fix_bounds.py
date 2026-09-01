#!/usr/bin/env python3
"""Fix tile.json bounds that were stored as EPSG:3857 metres."""

import json
import sys
from pathlib import Path

from app.services.preprocessor import parse_wgs84_bounds


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
        bounds = parse_wgs84_bounds(dataset)
        fix_tile_json(tile_json, bounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
