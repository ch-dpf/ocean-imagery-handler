"""Self-contained Python raster engine replacing GDAL CLI usage."""

from app.services.raster.errors import RasterError
from app.services.raster.info import raster_info_json, raster_info_text, wgs84_bounds
from app.services.raster.overviews import add_overviews
from app.services.raster.reproject import reproject_geotiff
from app.services.raster.tiles import generate_tiles

__all__ = [
    "RasterError",
    "add_overviews",
    "generate_tiles",
    "raster_info_json",
    "raster_info_text",
    "reproject_geotiff",
    "wgs84_bounds",
]
