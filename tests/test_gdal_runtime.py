"""Unit tests for the shared GDAL Python runtime adapter."""

from unittest.mock import Mock

import pytest

from app.services import gdal_runtime


class FakeGdal:
    def __init__(self, version_num: str = "3120000") -> None:
        self.version_num = version_num
        self.options: dict[str, str | None] = {}
        self.Run = Mock(return_value=Mock())

    def VersionInfo(self, key: str) -> str:
        return self.version_num if key == "VERSION_NUM" else "3.12.0"

    def GetConfigOption(self, key: str):
        return self.options.get(key)

    def SetConfigOption(self, key: str, value: str | None):
        if value is None:
            self.options.pop(key, None)
        else:
            self.options[key] = value


def test_require_gdal_rejects_old_version(monkeypatch):
    monkeypatch.setattr(gdal_runtime, "_gdal", FakeGdal("3110000"))
    with pytest.raises(gdal_runtime.GdalPythonError, match="GDAL >= 3.12"):
        gdal_runtime.require_gdal()


def test_run_algorithm_applies_config_and_scales_progress(monkeypatch):
    fake = FakeGdal()
    fake.options["GDAL_CACHEMAX"] = "64"
    monkeypatch.setattr(gdal_runtime, "_gdal", fake)
    progress = Mock()

    result = gdal_runtime.run_algorithm(
        ["raster", "tile"],
        {"input": "input.tif", "output": "tiles"},
        config={"GDAL_CACHEMAX": "512"},
        on_progress=progress,
    )

    assert result is fake.Run.return_value
    assert fake.options["GDAL_CACHEMAX"] == "64"
    assert fake.Run.call_args.args[0] == ["raster", "tile"]
    assert fake.Run.call_args.kwargs["arguments"]["output"] == "tiles"

    callback = fake.Run.call_args.kwargs["progress"]
    assert callback(0.25, "working", None) is True
    progress.assert_called_once_with(25.0, "working")


def test_run_algorithm_wraps_native_error(monkeypatch):
    fake = FakeGdal()
    fake.Run.side_effect = RuntimeError("native failure")
    monkeypatch.setattr(gdal_runtime, "_gdal", fake)

    with pytest.raises(gdal_runtime.GdalPythonError, match="native failure"):
        gdal_runtime.run_algorithm(["raster", "info"], {"input": "broken.tif"})
