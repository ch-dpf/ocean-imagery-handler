"""Windowed GeoTIFF read/write using tifffile (no GDAL)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import tifffile
from pyproj import CRS

from app.services.raster.affine import Affine
from app.services.raster.crsutil import crs_epsg, parse_crs
from app.services.raster.errors import RasterError

# GeoTIFF tags
_MODEL_PIXEL_SCALE = 33550
_MODEL_TIEPOINT = 33922
_MODEL_TRANSFORMATION = 34264
_GEO_KEY_DIRECTORY = 34735

_FULL_LOAD_MAX_BYTES = 128 * 1024 * 1024


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        inner = getattr(value, "value", None)
        if inner is None:
            return None
        try:
            return int(inner)
        except (TypeError, ValueError):
            return None


class _TileCache:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(int(max_bytes), 1)
        self._data: OrderedDict[int, np.ndarray] = OrderedDict()
        self._nbytes = 0

    def get(self, key: int) -> np.ndarray | None:
        array = self._data.get(key)
        if array is not None:
            self._data.move_to_end(key)
        return array

    def put(self, key: int, array: np.ndarray) -> None:
        nbytes = int(array.nbytes)
        if key in self._data:
            self._nbytes -= int(self._data[key].nbytes)
        self._data[key] = array
        self._nbytes += nbytes
        self._data.move_to_end(key)
        while self._nbytes > self.max_bytes and len(self._data) > 1:
            _, old = self._data.popitem(last=False)
            self._nbytes -= int(old.nbytes)


def _normalize_hwc(array: np.ndarray, samples: int) -> np.ndarray:
    array = np.asarray(array)
    while array.ndim > 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 2:
        array = array[:, :, np.newaxis]
    if array.ndim != 3:
        raise RasterError(f"Unexpected raster tile shape {array.shape}")
    if array.shape[2] >= samples:
        return array[:, :, :samples]
    padded = np.zeros((array.shape[0], array.shape[1], samples), dtype=array.dtype)
    padded[:, :, : array.shape[2]] = array
    return padded


def _geokey_directory(epsg: int, projected: bool) -> tuple[int, ...]:
    if projected:
        keys = (
            (1024, 0, 1, 1),  # ModelTypeProjected
            (1025, 0, 1, 1),  # RasterPixelIsArea
            (2048, 0, 1, 4326),
            (3072, 0, 1, int(epsg)),
            (3076, 0, 1, 9001),  # meter
        )
    else:
        keys = (
            (1024, 0, 1, 2),  # ModelTypeGeographic
            (1025, 0, 1, 1),
            (2048, 0, 1, int(epsg)),
            (2054, 0, 1, 9102),  # degree
        )
    header = (1, 1, 1, len(keys))
    flat: list[int] = list(header)
    for key in keys:
        flat.extend(key)
    return tuple(flat)


def geotiff_extratags(crs: CRS, affine: Affine) -> list[tuple]:
    epsg = crs_epsg(crs)
    if epsg is None:
        raise RasterError(f"Cannot write GeoTIFF without an EPSG code (got {crs.to_string()})")
    geokeys = _geokey_directory(epsg, projected=crs.is_projected)
    if abs(affine.b) < 1e-12 and abs(affine.d) < 1e-12:
        scale = (abs(affine.a), abs(affine.e), 0.0)
        tie = (0.0, 0.0, 0.0, float(affine.c), float(affine.f), 0.0)
        return [
            (_MODEL_PIXEL_SCALE, "d", 3, scale, True),
            (_MODEL_TIEPOINT, "d", 6, tie, True),
            (_GEO_KEY_DIRECTORY, "H", len(geokeys), geokeys, True),
        ]
    matrix = (
        float(affine.a),
        float(affine.b),
        0.0,
        float(affine.c),
        float(affine.d),
        float(affine.e),
        0.0,
        float(affine.f),
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    return [
        (_MODEL_TRANSFORMATION, "d", 16, matrix, True),
        (_GEO_KEY_DIRECTORY, "H", len(geokeys), geokeys, True),
    ]


def tiff_compression(compress: str, jpeg_quality: int) -> tuple[str | None, dict | None]:
    codec = compress.upper()
    if codec in {"NONE", "UNCOMPRESSED"}:
        return None, None
    if codec == "JPEG":
        return "jpeg", {"level": int(jpeg_quality)}
    if codec == "LZW":
        return "lzw", None
    if codec in {"DEFLATE", "ADOBE_DEFLATE", "ZLIB"}:
        return "zlib", {"level": 8}
    raise RasterError(f"Unsupported GeoTIFF compression: {compress}")


def _affine_from_geotiff_tags(tags: dict) -> Affine:
    transform = tags.get("ModelTransformation")
    if transform is not None:
        values = [float(v) for v in transform]
        if len(values) >= 8:
            return Affine(a=values[0], b=values[1], c=values[3], d=values[4], e=values[5], f=values[7])
    scale = tags.get("ModelPixelScale")
    tie = tags.get("ModelTiepoint")
    if scale is None or tie is None:
        raise RasterError("GeoTIFF is missing ModelPixelScale/ModelTiepoint georeferencing")
    sx, sy = float(scale[0]), float(scale[1])
    i, j = float(tie[0]), float(tie[1])
    x, y = float(tie[3]), float(tie[4])
    a = sx
    e = -sy
    c = x - a * i
    f = y - e * j
    return Affine(a=a, b=0.0, c=c, d=0.0, e=e, f=f)


def _crs_from_geotiff_tags(tags: dict) -> CRS:
    pcs = _as_int(tags.get("ProjectedCSTypeGeoKey"))
    gcs = _as_int(tags.get("GeographicTypeGeoKey"))
    if pcs and pcs not in {0, 32767}:
        return parse_crs(f"EPSG:{pcs}")
    if gcs and gcs not in {0, 32767}:
        return parse_crs(f"EPSG:{gcs}")
    for key in ("GTCitationGeoKey", "PCSCitationGeoKey", "GeogCitationGeoKey", "GeoAsciiParams"):
        raw = tags.get(key)
        if not raw:
            continue
        text = str(raw).strip().strip("|")
        if not text:
            continue
        try:
            return parse_crs(text)
        except RasterError:
            continue
    raise RasterError("GeoTIFF has no recognizable CRS GeoKeys")


class GeoTiffReader:
    """Windowed reader for a georeferenced TIFF."""

    def __init__(self, path: Path | str, cache_bytes: int = 512 * 1024 * 1024) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise RasterError(f"Raster not found: {self.path}")
        self._tif = tifffile.TiffFile(self.path)
        self._ovr_tif: tifffile.TiffFile | None = None
        try:
            if not self._tif.pages:
                raise RasterError(f"No TIFF pages in {self.path}")
            self._page = self._tif.pages[0]
            tags = self._page.geotiff_tags or {}
            if not tags:
                raise RasterError(f"Not a GeoTIFF (missing georeferencing): {self.path}")
            self.affine = _affine_from_geotiff_tags(tags)
            self.crs = _crs_from_geotiff_tags(tags)
            self.width = int(self._page.imagewidth)
            self.height = int(self._page.imagelength)
            self.samples = int(self._page.samplesperpixel or 1)
            self.dtype = np.dtype(self._page.dtype)
            self._tile_w = int(self._page.tilewidth or self.width)
            self._tile_h = int(self._page.tilelength or (self._page.rowsperstrip or self.height))
            if self._tile_w <= 0:
                self._tile_w = self.width
            if self._tile_h <= 0:
                self._tile_h = self.height
            self._tiles_across = max(1, (self.width + self._tile_w - 1) // self._tile_w)
            self._lock = threading.Lock()
            self._cache = _TileCache(cache_bytes)
            self._full: np.ndarray | None = None
            uncompressed = int(self.width) * int(self.height) * int(self.samples) * int(self.dtype.itemsize)
            if uncompressed <= min(cache_bytes, _FULL_LOAD_MAX_BYTES):
                try:
                    array = self._page.asarray()
                    self._full = _normalize_hwc(array, self.samples)
                except Exception:
                    self._full = None
            self._overviews: list[tuple[int, tifffile.TiffPage, Affine]] = []
            self._load_overviews()
        except Exception:
            self.close()
            raise

    def _load_overviews(self) -> None:
        for page in list(self._tif.pages)[1:]:
            self._maybe_add_overview(page, self.affine)
        ovr_path = Path(str(self.path) + ".ovr")
        if ovr_path.is_file():
            try:
                self._ovr_tif = tifffile.TiffFile(ovr_path)
            except Exception:
                self._ovr_tif = None
                return
            for page in self._ovr_tif.pages:
                self._maybe_add_overview(page, self.affine)

    def _maybe_add_overview(self, page: tifffile.TiffPage, base: Affine) -> None:
        width = int(page.imagewidth)
        if width <= 0 or width >= self.width:
            return
        scale = max(1, int(round(self.width / width)))
        if scale <= 1:
            return
        self._overviews.append((scale, page, base.scaled(scale)))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = []
        ys = []
        for col, row in ((0, 0), (self.width, 0), (self.width, self.height), (0, self.height)):
            x, y = self.affine.xy(col, row)
            xs.append(float(x))
            ys.append(float(y))
        return min(xs), min(ys), max(xs), max(ys)

    def close(self) -> None:
        if getattr(self, "_ovr_tif", None) is not None:
            try:
                self._ovr_tif.close()
            except Exception:
                pass
            self._ovr_tif = None
        tif = getattr(self, "_tif", None)
        if tif is not None:
            try:
                tif.close()
            except Exception:
                pass
            self._tif = None  # type: ignore[assignment]

    def __enter__(self) -> GeoTiffReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _decode_index(self, page: tifffile.TiffPage, index: int) -> np.ndarray:
        offsets = page.dataoffsets
        counts = page.databytecounts
        if index >= len(offsets) or counts[index] == 0:
            th = int(page.tilelength or page.rowsperstrip or page.imagelength)
            tw = int(page.tilewidth or page.imagewidth)
            return np.zeros((th, tw, self.samples), dtype=self.dtype)
        handle = page.parent.filehandle
        handle.seek(offsets[index])
        blob = handle.read(counts[index])
        decoded = page.decode(blob, index)[0]
        return _normalize_hwc(decoded, self.samples)

    def _get_tile(self, ty: int, tx: int) -> np.ndarray:
        index = ty * self._tiles_across + tx
        cached = self._cache.get(index)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(index)
            if cached is not None:
                return cached
            array = self._decode_index(self._page, index)
            self._cache.put(index, array)
            return array

    def read_window(self, row0: int, col0: int, height: int, width: int) -> np.ndarray:
        """Return (height, width, samples) filled with zeros outside the image."""
        out = np.zeros((height, width, self.samples), dtype=self.dtype)
        img_r0 = max(0, row0)
        img_c0 = max(0, col0)
        img_r1 = min(self.height, row0 + height)
        img_c1 = min(self.width, col0 + width)
        if img_r0 >= img_r1 or img_c0 >= img_c1:
            return out

        if self._full is not None:
            out[img_r0 - row0 : img_r1 - row0, img_c0 - col0 : img_c1 - col0] = self._full[
                img_r0:img_r1, img_c0:img_c1
            ]
            return out

        tile_r0 = img_r0 // self._tile_h
        tile_r1 = (img_r1 - 1) // self._tile_h
        tile_c0 = img_c0 // self._tile_w
        tile_c1 = (img_c1 - 1) // self._tile_w
        for ty in range(tile_r0, tile_r1 + 1):
            for tx in range(tile_c0, tile_c1 + 1):
                tile = self._get_tile(ty, tx)
                y0 = ty * self._tile_h
                x0 = tx * self._tile_w
                isy0 = max(img_r0, y0)
                isx0 = max(img_c0, x0)
                isy1 = min(img_r1, y0 + tile.shape[0])
                isx1 = min(img_c1, x0 + tile.shape[1])
                if isy0 >= isy1 or isx0 >= isx1:
                    continue
                out[isy0 - row0 : isy1 - row0, isx0 - col0 : isx1 - col0] = tile[
                    isy0 - y0 : isy1 - y0, isx0 - x0 : isx1 - x0
                ]
        return out


def write_geotiff_tiled(
    path: Path,
    tiles: Iterator[np.ndarray],
    *,
    shape: tuple[int, int, int],
    affine: Affine,
    crs: CRS,
    compress: str,
    block_size: int,
    jpeg_quality: int = 85,
    dtype: np.dtype | type = np.uint8,
) -> None:
    """Write a tiled GeoTIFF from a row-major iterator of full-size tiles."""
    height, width, samples = shape
    if samples not in {1, 2, 3, 4}:
        raise RasterError(f"Unsupported band count {samples}")
    codec, codec_args = tiff_compression(compress, jpeg_quality)
    photometric = "minisblack" if samples <= 2 else "rgb"
    extrasamples = None
    if samples in {2, 4}:
        extrasamples = "unassalpha"
    write_shape: tuple[int, ...]
    if samples == 1:
        write_shape = (height, width)
    else:
        write_shape = (height, width, samples)

    def _iter() -> Iterator[np.ndarray]:
        for tile in tiles:
            if samples == 1 and tile.ndim == 3:
                yield tile[:, :, 0]
            else:
                yield tile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        kwargs: dict = {
            "shape": write_shape,
            "dtype": np.dtype(dtype),
            "photometric": photometric,
            "tile": (block_size, block_size),
            "extratags": geotiff_extratags(crs, affine),
            "software": "ocean-imagery-handler",
            "metadata": None,
        }
        if extrasamples is not None:
            kwargs["extrasamples"] = extrasamples
        if codec is not None:
            kwargs["compression"] = codec
            if codec_args:
                kwargs["compressionargs"] = codec_args
        tif.write(_iter(), **kwargs)


def write_geotiff_array(
    path: Path,
    data: np.ndarray,
    *,
    affine: Affine,
    crs: CRS,
    compress: str = "DEFLATE",
    block_size: int = 256,
    jpeg_quality: int = 85,
) -> None:
    """Write an in-memory HWC (or HW) array as a tiled GeoTIFF."""
    if data.ndim == 2:
        data = data[:, :, np.newaxis]
    if data.ndim != 3:
        raise RasterError(f"Expected HWC array, got shape {data.shape}")
    height, width, samples = data.shape
    tile_h = block_size
    tile_w = block_size
    n_ty = (height + tile_h - 1) // tile_h
    n_tx = (width + tile_w - 1) // tile_w

    def tiles() -> Iterator[np.ndarray]:
        for ty in range(n_ty):
            for tx in range(n_tx):
                r0 = ty * tile_h
                c0 = tx * tile_w
                tile = np.zeros((tile_h, tile_w, samples), dtype=data.dtype)
                sl = data[r0 : min(r0 + tile_h, height), c0 : min(c0 + tile_w, width)]
                tile[: sl.shape[0], : sl.shape[1]] = sl
                yield tile

    write_geotiff_tiled(
        path,
        tiles(),
        shape=(height, width, samples),
        affine=affine,
        crs=crs,
        compress=compress,
        block_size=block_size,
        jpeg_quality=jpeg_quality,
        dtype=data.dtype,
    )
