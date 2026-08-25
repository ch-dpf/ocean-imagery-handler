#!/usr/bin/env python3
"""Fix imagery.json bounds that were stored as EPSG:3857 metres."""

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


def fix_imagery_json(imagery_json: Path, bounds: list[float]) -> None:
    data = json.loads(imagery_json.read_text(encoding="utf-8"))
    data["bounds"] = bounds
    if isinstance(data.get("cesium"), dict):
        data["cesium"]["rectangle"] = bounds
    imagery_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Updated {imagery_json} -> {bounds}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: fix_bounds.py <job_id> [<job_id>...]")
        return 1

    workspace = Path("/data/workspace")
    for job_id in sys.argv[1:]:
        job_dir = workspace / "jobs" / job_id
        dataset = job_dir / "preprocess" / "preprocessed.tif"
        imagery_json = job_dir / "tiles" / "imagery.json"
        if not dataset.is_file():
            print(f"Skip {job_id}: missing {dataset}")
            continue
        if not imagery_json.is_file():
            print(f"Skip {job_id}: missing {imagery_json}")
            continue
        bounds = wgs84_bounds(dataset)
        fix_imagery_json(imagery_json, bounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
