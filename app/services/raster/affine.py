"""2D affine geotransform (GDAL-style [c, a, b, f, d, e])."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Affine:
    """Maps pixel coordinates to CRS units.

    x = a * col + b * row + c
    y = d * col + e * row + f

    PixelIsArea: (0, 0) is the upper-left corner of the upper-left pixel.
    """

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @classmethod
    def from_gdal(cls, gt: tuple[float, float, float, float, float, float] | list[float]) -> Affine:
        c, a, b, f, d, e = (float(v) for v in gt)
        return cls(a=a, b=b, c=c, d=d, e=e, f=f)

    def to_gdal(self) -> list[float]:
        return [self.c, self.a, self.b, self.f, self.d, self.e]

    @classmethod
    def north_up(cls, origin_x: float, origin_y: float, pixel_width: float, pixel_height: float) -> Affine:
        """North-up transform; ``pixel_height`` is the positive ground size of one pixel."""
        return cls(a=float(pixel_width), b=0.0, c=float(origin_x), d=0.0, e=-float(pixel_height), f=float(origin_y))

    def xy(self, col: np.ndarray | float, row: np.ndarray | float) -> tuple[np.ndarray | float, np.ndarray | float]:
        return self.a * col + self.b * row + self.c, self.d * col + self.e * row + self.f

    def colrow(self, x: np.ndarray | float, y: np.ndarray | float) -> tuple[np.ndarray | float, np.ndarray | float]:
        det = self.a * self.e - self.b * self.d
        if abs(det) < 1e-30:
            raise ValueError("affine transform is not invertible")
        ic = x - self.c
        ir = y - self.f
        col = (self.e * ic - self.b * ir) / det
        row = (-self.d * ic + self.a * ir) / det
        return col, row

    def scaled(self, factor: float) -> Affine:
        """Pixel size multiplied by ``factor`` (overview level), origin unchanged."""
        return Affine(
            a=self.a * factor,
            b=self.b * factor,
            c=self.c,
            d=self.d * factor,
            e=self.e * factor,
            f=self.f,
        )

    def is_north_up(self, tol: float = 1e-8) -> bool:
        return abs(self.b) <= tol and abs(self.d) <= tol and abs(self.a) > tol and abs(self.e) > tol

    @property
    def pixel_width(self) -> float:
        return float(np.hypot(self.a, self.d))

    @property
    def pixel_height(self) -> float:
        return float(np.hypot(self.b, self.e))
