"""Smoke-test the in-process GDAL preprocessing and imagery tiling pipeline."""

from __future__ import annotations

import tempfile
from pathlib import Path

from osgeo import gdal, osr

from app.schemas import (
    PreprocessOptions,
    TileFormat,
    TileProfile,
    TileScheme,
    TilingOptions,
)
from app.services.preprocessor import parse_wgs84_bounds, preprocess_imagery
from app.services.tile_publisher import publish_tileset
from app.services.tiler_runner import run_raster_tile


def _create_source(path: Path) -> None:
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), 512, 512, 3, gdal.GDT_Byte
    )
    if dataset is None:
        raise RuntimeError("Unable to create smoke-test raster")

    dataset.SetGeoTransform((120.0, 0.001, 0.0, 31.0, 0.0, -0.001))
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_ref.ExportToWkt())
    for index, value in enumerate((60, 120, 180), start=1):
        dataset.GetRasterBand(index).Fill(value)
    dataset = None


def main() -> None:
    gdal.UseExceptions()
    with tempfile.TemporaryDirectory(prefix="imagery-gdal-python-") as temp_dir:
        root = Path(temp_dir)
        source = root / "source.tif"
        _create_source(source)

        progress: list[tuple[float, str | None]] = []
        preprocessed = preprocess_imagery(
            source,
            root / "preprocess",
            PreprocessOptions(white_as_transparent=True),
            128,
            on_subprogress=lambda percent, message: progress.append((percent, message)),
        )
        bounds = parse_wgs84_bounds(preprocessed, {"GDAL_CACHEMAX": "128"})
        run_raster_tile(
            preprocessed,
            root / "tiles",
            TilingOptions(start_zoom=2, end_zoom=0, thread_count=2),
            128,
            on_subprogress=lambda percent, message: progress.append((percent, message)),
        )

        output = gdal.Open(str(preprocessed))
        if output is None:
            raise RuntimeError("Preprocessed raster cannot be opened")
        if "3857" not in output.GetProjection():
            raise RuntimeError("Preprocessed raster is not EPSG:3857")
        if output.RasterCount != 4:
            raise RuntimeError(f"Expected RGBA output, got {output.RasterCount} bands")

        overview_count = output.GetRasterBand(1).GetOverviewCount()
        if overview_count != 4:
            raise RuntimeError(f"Expected 4 overview levels, got {overview_count}")
        if not (119.9 < bounds[0] < bounds[2] < 120.7):
            raise RuntimeError(f"Unexpected longitude bounds: {bounds}")
        if not (30.4 < bounds[1] < bounds[3] < 31.1):
            raise RuntimeError(f"Unexpected latitude bounds: {bounds}")

        tiles = sorted((root / "tiles").rglob("*.png"))
        if not tiles:
            raise RuntimeError("No PNG tiles were generated")
        if not progress or progress[-1][0] != 100.0:
            raise RuntimeError("Progress callback did not reach 100%")

        imagery_url, tileset_name, url_template = publish_tileset(
            job_id="smoke-job",
            tiles_dir=root / "tiles",
            tilesets_dir=root / "tilesets",
            public_url="http://imagery.example",
            base_path="/imagery",
            profile=TileProfile.MERCATOR,
            tile_format=TileFormat.PNG,
            bounds_wgs84=bounds,
            tile_scheme=TileScheme.XYZ,
        )
        if not (root / "tiles" / "tile.json").is_file():
            raise RuntimeError("TileJSON metadata was not generated")
        if not (root / "tilesets" / tileset_name).is_symlink():
            raise RuntimeError("Published tileset symlink was not registered")
        if imagery_url != "http://imagery.example/imagery/smoke-job":
            raise RuntimeError(f"Unexpected imagery URL: {imagery_url}")
        if not url_template.endswith("/{z}/{x}/{y}.png"):
            raise RuntimeError(f"Unexpected tile URL template: {url_template}")

        print(
            {
                "gdal": gdal.VersionInfo("RELEASE_NAME"),
                "bounds": bounds,
                "tiles": len(tiles),
                "overviews": overview_count,
                "progress_events": len(progress),
                "tilejson": True,
                "published": True,
            }
        )


if __name__ == "__main__":
    main()
