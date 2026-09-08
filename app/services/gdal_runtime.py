"""Shared helpers for running GDAL algorithms in the worker process."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

try:
    from osgeo import gdal as _gdal
    from osgeo import osr as _osr
except ImportError as exc:  # pragma: no cover - exercised on hosts without GDAL
    _gdal = None
    _osr = None
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None
    _gdal.UseExceptions()

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str | None], None]


class GdalPythonError(RuntimeError):
    """Raised when the GDAL Python runtime or an algorithm is unavailable."""


_CONFIG_LOCK = threading.RLock()


def require_gdal() -> Any:
    """Return the GDAL module, requiring the 3.12 algorithm API."""
    if _gdal is None:
        raise GdalPythonError(
            "GDAL Python bindings are unavailable; install bindings matching libgdal 3.12"
        ) from _IMPORT_ERROR

    version_num = int(_gdal.VersionInfo("VERSION_NUM"))
    if version_num < 3_120_000:
        raise GdalPythonError(
            f"GDAL >= 3.12 is required; found {_gdal.VersionInfo('RELEASE_NAME')}"
        )
    return _gdal


def require_osr() -> Any:
    """Return the OSR module from the same GDAL Python installation."""
    require_gdal()
    if _osr is None:  # defensive: osgeo normally provides both modules
        raise GdalPythonError("GDAL OSR Python bindings are unavailable")
    return _osr


def _progress_adapter(callback: ProgressCallback | None):
    if callback is None:
        return None

    def _progress(complete: float, message: str, _user_data: object) -> bool:
        callback(min(max(float(complete) * 100.0, 0.0), 100.0), message or None)
        return True

    return _progress


@contextmanager
def gdal_config(options: Mapping[str, str] | None = None) -> Iterator[Any]:
    """Apply process-global GDAL config options for one synchronous operation."""
    module = require_gdal()
    normalized = {str(key): str(value) for key, value in (options or {}).items()}

    # GDAL configuration is process-global. Holding the lock for the operation
    # prevents concurrent Python tasks from observing another task's settings.
    with _CONFIG_LOCK:
        previous = {key: module.GetConfigOption(key) for key in normalized}
        try:
            for key, value in normalized.items():
                module.SetConfigOption(key, value)
            yield module
        finally:
            for key, value in previous.items():
                module.SetConfigOption(key, value)


def run_algorithm(
    algorithm_path: Sequence[str],
    arguments: Mapping[str, object],
    *,
    config: Mapping[str, str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> Any:
    """Run a GDAL algorithm synchronously and return its Algorithm instance."""
    path = [str(part) for part in algorithm_path]
    logger.info("Running GDAL Python algorithm %s", " ".join(path))
    try:
        with gdal_config(config) as module:
            result = module.Run(
                path,
                arguments=dict(arguments),
                progress=_progress_adapter(on_progress),
            )
    except GdalPythonError:
        raise
    except Exception as exc:
        raise GdalPythonError(f"GDAL {' '.join(path)} failed: {exc}") from exc

    if result is None:
        raise GdalPythonError(f"GDAL {' '.join(path)} returned no algorithm result")
    return result


def finalize_algorithm(algorithm: Any) -> None:
    """Release references held by a completed Algorithm instance."""
    finalize = getattr(algorithm, "Finalize", None)
    if callable(finalize):
        finalize()


def algorithm_argument_names(algorithm_path: Sequence[str]) -> set[str]:
    """Return supported argument names, primarily for startup diagnostics."""
    module = require_gdal()
    algorithm = module.Algorithm(*algorithm_path)
    if algorithm is None:
        raise GdalPythonError(f"GDAL algorithm is unavailable: {' '.join(algorithm_path)}")
    return set(algorithm.GetArgNames())
