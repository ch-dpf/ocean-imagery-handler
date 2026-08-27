"""Progress calibration tests."""

import json

from app.services.progress_calibration import (
    DEFAULT_WEIGHTS_WITH_PUBLISH,
    ProgressCalibrationStore,
    build_stage_ranges,
    durations_to_ratios,
    merge_weight_ema,
    normalize_weights,
)


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value


class _FakeSettings:
    progress_calibration_min_samples = 3
    progress_calibration_ema_alpha = 0.5


def test_build_stage_ranges_from_weights():
    ranges = build_stage_ranges(
        {
            "initializing": 0.10,
            "gdal_preprocess": 0.20,
            "gdal_raster_tile": 0.60,
            "register_tileset": 0.10,
        }
    )
    assert ranges["initializing"] == (0.0, 10.0)
    assert ranges["gdal_preprocess"] == (10.0, 30.0)
    assert ranges["gdal_raster_tile"] == (30.0, 90.0)
    assert ranges["register_tileset"] == (90.0, 100.0)


def test_durations_to_ratios():
    ratios = durations_to_ratios(
        {
            "initializing": 1.0,
            "gdal_preprocess": 9.0,
            "gdal_raster_tile": 90.0,
        },
        ("initializing", "gdal_preprocess", "gdal_raster_tile"),
    )
    assert ratios["gdal_raster_tile"] == 0.9


def test_merge_weight_ema_normalizes():
    merged = merge_weight_ema(
        {"gdal_preprocess": 0.2, "gdal_raster_tile": 0.8},
        {"gdal_preprocess": 0.1, "gdal_raster_tile": 0.9},
        alpha=0.5,
    )
    assert abs(sum(merged.values()) - 1.0) < 1e-6
    assert merged["gdal_raster_tile"] > merged["gdal_preprocess"]


def test_calibration_uses_default_until_min_samples():
    store = ProgressCalibrationStore(_FakeSettings(), _FakeRedis())  # type: ignore[arg-type]
    ranges, source, samples = store.get_stage_ranges(auto_publish=True)
    assert source == "default"
    assert samples == 0
    assert ranges["gdal_raster_tile"] == (25.0, 95.0)


def test_calibration_switches_to_historical_after_enough_samples():
    redis = _FakeRedis()
    settings = _FakeSettings()
    calibration = ProgressCalibrationStore(settings, redis)  # type: ignore[arg-type]

    for _ in range(3):
        calibration.record_job_durations(
            {
                "initializing": 1.0,
                "gdal_preprocess": 9.0,
                "gdal_raster_tile": 80.0,
                "register_tileset": 10.0,
            },
            auto_publish=True,
        )

    ranges, source, samples = calibration.get_stage_ranges(auto_publish=True)
    assert source == "historical"
    assert samples == 3
    default_tiling_span = (
        build_stage_ranges(DEFAULT_WEIGHTS_WITH_PUBLISH)["gdal_raster_tile"][1]
        - build_stage_ranges(DEFAULT_WEIGHTS_WITH_PUBLISH)["gdal_raster_tile"][0]
    )
    calibrated_tiling_span = ranges["gdal_raster_tile"][1] - ranges["gdal_raster_tile"][0]
    assert calibrated_tiling_span > default_tiling_span

    payload = json.loads(redis.get("imagery:progress:calibration"))
    assert payload["with_publish"]["sample_count"] == 3


def test_normalize_weights():
    normalized = normalize_weights({"a": 2.0, "b": 8.0})
    assert normalized == {"a": 0.2, "b": 0.8}
