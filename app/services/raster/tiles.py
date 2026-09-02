"""XYZ / TMS / raster tile generation (replacement for ``gdal raster tile``)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

from app.schemas import TileFormat, TileProfile, TileScheme, TilingOptions
from app.services.raster.affine import Affine
from app.services.raster.crsutil import (
    EARTH_HALF,
    WEB_MERCATOR_MAX_LAT,
    crs_epsg,
    destination_pixel_size,
    make_transformer,
    parse_crs,
    wgs84_bounds_from_rect,
)
from app.services.raster.geotiff import GeoTiffReader
from app.services.raster.kml import write_doc_kml
from app.services.raster.parallel import default_workers, run_unordered
from app.services.raster.resample import array_to_image, image_to_array, normalize_resampling, resize_array
from app.services.raster.reproject import destination_sample_count
from app.services.raster.warp import warp_window

ProgressFn = Callable[[float, str | None], None]


@dataclass(frozen=True, slots=True)
class RasterExtent:
    """CRS grid used to plan tiles without opening a GeoTIFF."""

    crs: CRS
    affine: Affine
    width: int
    height: int
    samples: int = 3

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs: list[float] = []
        ys: list[float] = []
        for col, row in ((0, 0), (self.width, 0), (self.width, self.height), (0, self.height)):
            x, y = self.affine.xy(col, row)
            xs.append(float(x))
            ys.append(float(y))
        return min(xs), min(ys), max(xs), max(ys)


def tile_sample_count(profile: TileProfile, dest_samples: int) -> int:
    """uint8 bands produced for one output tile."""
    if profile == TileProfile.RASTER:
        return min(int(dest_samples), 4)
    return destination_sample_count(dest_samples, add_alpha=True, white_as_transparent=False)


def _tile_ext(fmt: TileFormat) -> str:
    if fmt == TileFormat.JPEG:
        return "jpg"
    if fmt == TileFormat.WEBP:
        return "webp"
    return "png"


def lonlat_to_mercator_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = min(max(lat, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)
    n = 2**z
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n))
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def mercator_tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    size = (2 * EARTH_HALF) / n
    minx = -EARTH_HALF + x * size
    maxx = -EARTH_HALF + (x + 1) * size
    maxy = EARTH_HALF - y * size
    miny = EARTH_HALF - (y + 1) * size
    return minx, miny, maxx, maxy


def geodetic_tile_bounds_4326(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n_x = 2 ** (z + 1)
    n_y = 2**z
    west = -180.0 + x * 360.0 / n_x
    east = -180.0 + (x + 1) * 360.0 / n_x
    north = 90.0 - y * 180.0 / n_y
    south = 90.0 - (y + 1) * 180.0 / n_y
    return west, south, east, north


def auto_max_zoom_mercator(pixel_size_m: float, tile_size: int) -> int:
    if pixel_size_m <= 0:
        return 0
    ratio = (2 * EARTH_HALF) / (pixel_size_m * tile_size)
    if ratio <= 1.0:
        return 0
    return max(0, math.ceil(math.log2(ratio) - 1e-12))


def auto_max_zoom_geodetic(pixel_size_deg: float, tile_size: int) -> int:
    if pixel_size_deg <= 0:
        return 0
    ratio = 180.0 / (pixel_size_deg * tile_size)
    if ratio <= 1.0:
        return 0
    return max(0, math.ceil(math.log2(ratio) - 1e-12))


def auto_max_zoom_raster(width: int, height: int, tile_size: int) -> int:
    longest = max(width, height)
    if longest <= tile_size:
        return 0
    return max(0, math.ceil(math.log2(longest / tile_size)))


def compute_max_zoom(src: GeoTiffReader | RasterExtent, options: TilingOptions) -> int:
    if options.start_zoom is not None:
        return options.start_zoom
    if options.profile == TileProfile.RASTER:
        return auto_max_zoom_raster(src.width, src.height, options.tile_size)
    if options.profile == TileProfile.GEODETIC:
        if crs_epsg(src.crs) == 4326:
            px = min(src.affine.pixel_width, src.affine.pixel_height)
        else:
            px, py = destination_pixel_size(
                src.crs, parse_crs("EPSG:4326"), src.affine, src.width, src.height
            )
            px = min(px, py)
        return auto_max_zoom_geodetic(px, options.tile_size)
    if crs_epsg(src.crs) == 3857:
        px = min(src.affine.pixel_width, src.affine.pixel_height)
    else:
        px, py = destination_pixel_size(
            src.crs, parse_crs("EPSG:3857"), src.affine, src.width, src.height
        )
        px = min(px, py)
    return auto_max_zoom_mercator(px, options.tile_size)


def _xyz_range_mercator(bounds_wgs84: list[float], z: int) -> tuple[int, int, int, int]:
    west, south, east, north = bounds_wgs84
    x0, y0 = lonlat_to_mercator_tile(west, north, z)
    x1, y1 = lonlat_to_mercator_tile(east, south, z)
    n = 2**z
    return max(0, min(x0, x1)), min(n - 1, max(x0, x1)), max(0, min(y0, y1)), min(n - 1, max(y0, y1))


def _xyz_range_geodetic(bounds_wgs84: list[float], z: int) -> tuple[int, int, int, int]:
    west, south, east, north = bounds_wgs84
    n_x = 2 ** (z + 1)
    n_y = 2**z
    x0 = int(math.floor((west + 180.0) / 360.0 * n_x))
    x1 = int(math.floor((east + 180.0) / 360.0 * n_x))
    y0 = int(math.floor((90.0 - north) / 180.0 * n_y))
    y1 = int(math.floor((90.0 - south) / 180.0 * n_y))
    return max(0, min(x0, x1)), min(n_x - 1, max(x0, x1)), max(0, min(y0, y1)), min(n_y - 1, max(y0, y1))


def list_tiles(src: GeoTiffReader | RasterExtent, options: TilingOptions, z: int) -> list[tuple[int, int]]:
    """Return XYZ (x, y) tiles intersecting the raster at zoom z."""
    if options.profile == TileProfile.RASTER:
        max_zoom = compute_max_zoom(src, options) if options.start_zoom is None else options.start_zoom
        n = 2**z
        tiles: list[tuple[int, int]] = []
        scale = 2 ** max(0, max_zoom - z)
        src_tile = options.tile_size * scale
        max_x = (src.width + src_tile - 1) // src_tile
        max_y = (src.height + src_tile - 1) // src_tile
        for x in range(min(n, max_x)):
            for y in range(min(n, max_y)):
                tiles.append((x, y))
        return tiles

    bounds = wgs84_bounds_from_rect(src.crs, src.bounds)
    if options.profile == TileProfile.GEODETIC:
        x0, x1, y0, y1 = _xyz_range_geodetic(bounds, z)
    else:
        x0, x1, y0, y1 = _xyz_range_mercator(bounds, z)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def count_output_tiles(src: GeoTiffReader | RasterExtent, options: TilingOptions) -> int:
    min_zoom = options.end_zoom
    max_zoom = compute_max_zoom(src, options)
    if max_zoom < min_zoom:
        max_zoom = min_zoom
    return sum(len(list_tiles(src, options, z)) for z in range(min_zoom, max_zoom + 1))


def _file_y(z: int, y_xyz: int, scheme: TileScheme) -> int:
    if scheme == TileScheme.TMS:
        return (2**z - 1) - y_xyz
    return y_xyz


def _tile_path(output_dir: Path, z: int, x: int, y_xyz: int, scheme: TileScheme, ext: str) -> Path:
    return output_dir / str(z) / str(x) / f"{_file_y(z, y_xyz, scheme)}.{ext}"


def _save_tile(path: Path, array: np.ndarray, fmt: TileFormat) -> bool:
    """Write one tile file.

    Fully transparent / zero-valued tiles are still written. That matches
    ``gdal raster tile`` without ``--skip-blank`` and keeps low-zoom pyramid
    entries for sparse or sub-pixel footprints so ``tile.json`` minzoom can
    reach ``end_zoom``.
    """
    if array.size == 0:
        return False
    if array.ndim == 2:
        array = array[:, :, np.newaxis]
    image = array_to_image(array)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == TileFormat.JPEG:
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, (0, 0, 0))
            background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(path, format="JPEG", quality=85)
    elif fmt == TileFormat.WEBP:
        image.save(path, format="WEBP", quality=85)
    else:
        image.save(path, format="PNG")
    return True


def _render_scheme_tile(
    src: GeoTiffReader,
    options: TilingOptions,
    z: int,
    x: int,
    y: int,
    resampling: str,
    transformer_mercator: Transformer | None = None,
    transformer_geodetic: Transformer | None = None,
) -> np.ndarray:
    tile_size = options.tile_size
    if options.profile == TileProfile.RASTER:
        max_zoom = options.start_zoom if options.start_zoom is not None else auto_max_zoom_raster(
            src.width, src.height, tile_size
        )
        scale = 2 ** max(0, max_zoom - z)
        src_tile = tile_size * scale
        col0 = x * src_tile
        row0 = y * src_tile
        level = src.select_level(float(src_tile), float(src_tile), tile_size, tile_size)
        if level.width != src.width or level.height != src.height:
            sx = level.width / src.width
            sy = level.height / src.height
            r0 = int(math.floor(row0 * sy))
            c0 = int(math.floor(col0 * sx))
            r1 = int(math.ceil((row0 + src_tile) * sy))
            c1 = int(math.ceil((col0 + src_tile) * sx))
            window = src.read_window(r0, c0, max(1, r1 - r0), max(1, c1 - c0), level=level)
        else:
            window = src.read_window(row0, col0, src_tile, src_tile)
        return resize_array(window, tile_size, tile_size, resampling)

    if options.profile == TileProfile.GEODETIC:
        west, south, east, north = geodetic_tile_bounds_4326(z, x, y)
        dst_crs = parse_crs("EPSG:4326")
        dst_affine = Affine.north_up(west, north, (east - west) / tile_size, (north - south) / tile_size)
        transformer = transformer_geodetic
    else:
        minx, miny, maxx, maxy = mercator_tile_bounds_3857(z, x, y)
        dst_crs = parse_crs("EPSG:3857")
        dst_affine = Affine.north_up(minx, maxy, (maxx - minx) / tile_size, (maxy - miny) / tile_size)
        transformer = transformer_mercator

    return warp_window(
        src,
        dst_affine,
        dst_crs,
        0,
        0,
        tile_size,
        tile_size,
        resampling,
        add_alpha=True,
        white_as_transparent=False,
        transformer=transformer,
    )


def _mosaic_children(
    output_dir: Path,
    z: int,
    x: int,
    y: int,
    options: TilingOptions,
    ext: str,
    resampling: str,
) -> np.ndarray | None:
    child_z = z + 1
    tile_size = options.tile_size
    canvas = None
    found = False
    for dy in range(2):
        for dx in range(2):
            child = _tile_path(output_dir, child_z, x * 2 + dx, y * 2 + dy, options.tile_scheme, ext)
            if not child.is_file():
                continue
            with Image.open(child) as image:
                arr = image_to_array(np.array(image))
            if canvas is None:
                bands = arr.shape[2]
                canvas = np.zeros((tile_size * 2, tile_size * 2, bands), dtype=np.uint8)
            h = min(tile_size, arr.shape[0])
            w = min(tile_size, arr.shape[1])
            canvas[dy * tile_size : dy * tile_size + h, dx * tile_size : dx * tile_size + w, : arr.shape[2]] = arr[:h, :w]
            found = True
    if not found or canvas is None:
        return None
    return resize_array(canvas, tile_size, tile_size, resampling)


def generate_tiles(
    input_path: Path,
    output_dir: Path,
    options: TilingOptions,
    *,
    cache_bytes: int,
    on_progress: ProgressFn | None = None,
) -> None:
    resampling = normalize_resampling(options.resampling_method)
    ext = _tile_ext(options.tile_format)
    workers = options.thread_count or default_workers()
    resume = bool(options.resume)

    with GeoTiffReader(input_path, cache_bytes=cache_bytes) as src:
        min_zoom = options.end_zoom
        max_zoom = compute_max_zoom(src, options)
        if max_zoom < min_zoom:
            max_zoom = min_zoom

        per_zoom: dict[int, list[tuple[int, int]]] = {}
        total_tiles = 0
        for z in range(min_zoom, max_zoom + 1):
            tiles = list_tiles(src, options, z)
            per_zoom[z] = tiles
            total_tiles += len(tiles)
        tile_samples = tile_sample_count(options.profile, src.samples)
        tile_bytes = options.tile_size * options.tile_size * tile_samples
        planned_bytes = max(1, total_tiles * tile_bytes)
        done_bytes = 0

        def _emit(z: int) -> None:
            nonlocal done_bytes
            done_bytes += tile_bytes
            if on_progress is not None:
                on_progress(100.0 * done_bytes / planned_bytes, f"Zoom {z}")

        dst_crs_3857 = parse_crs("EPSG:3857")
        dst_crs_4326 = parse_crs("EPSG:4326")
        transformer_mercator = (
            None if crs_epsg(src.crs) == 3857 else make_transformer(dst_crs_3857, src.crs)
        )
        transformer_geodetic = (
            None if crs_epsg(src.crs) == 4326 else make_transformer(dst_crs_4326, src.crs)
        )

        def render_one(z: int, x: int, y: int) -> None:
            path = _tile_path(output_dir, z, x, y, options.tile_scheme, ext)
            if resume and path.is_file() and path.stat().st_size > 0:
                return
            array = _render_scheme_tile(
                src,
                options,
                z,
                x,
                y,
                resampling,
                transformer_mercator=transformer_mercator,
                transformer_geodetic=transformer_geodetic,
            )
            _save_tile(path, array, options.tile_format)

        max_tiles = per_zoom.get(max_zoom, [])
        run_unordered(
            max_tiles,
            lambda xy: render_one(max_zoom, xy[0], xy[1]),
            workers=workers,
            on_done=lambda: _emit(max_zoom),
        )

        for z in range(max_zoom - 1, min_zoom - 1, -1):
            level_tiles = per_zoom.get(z, [])

            def render_parent(xy: tuple[int, int], zoom: int = z) -> None:
                px, py = xy
                path = _tile_path(output_dir, zoom, px, py, options.tile_scheme, ext)
                if resume and path.is_file() and path.stat().st_size > 0:
                    return
                array = _mosaic_children(output_dir, zoom, px, py, options, ext, resampling)
                if array is None:
                    array = _render_scheme_tile(
                        src,
                        options,
                        zoom,
                        px,
                        py,
                        resampling,
                        transformer_mercator=transformer_mercator,
                        transformer_geodetic=transformer_geodetic,
                    )
                _save_tile(path, array, options.tile_format)

            run_unordered(
                level_tiles,
                render_parent,
                workers=workers,
                on_done=lambda zoom=z: _emit(zoom),
            )

        if options.kml:
            kml_tiles = [(min_zoom, x, y) for x, y in per_zoom.get(min_zoom, [])]
            write_doc_kml(
                output_dir,
                kml_tiles,
                ext=ext,
                profile=options.profile.value,
                scheme=options.tile_scheme.value,
            )

    if on_progress is not None:
        on_progress(100.0, "Tiling complete")
