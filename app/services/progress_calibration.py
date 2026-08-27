"""Historical stage-duration calibration for overall job progress."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

CALIBRATION_KEY = "imagery:progress:calibration"

TIMED_STAGES_WITH_PUBLISH = (
    "initializing",
    "gdal_preprocess",
    "gdal_raster_tile",
    "register_tileset",
)
TIMED_STAGES_WITHOUT_PUBLISH = (
    "initializing",
    "gdal_preprocess",
    "gdal_raster_tile",
)

# Default stage time fractions (sum to 1.0), derived from original fixed ranges.
DEFAULT_WEIGHTS_WITH_PUBLISH: dict[str, float] = {
    "initializing": 0.02,
    "gdal_preprocess": 0.23,
    "gdal_raster_tile": 0.70,
    "register_tileset": 0.05,
}
DEFAULT_WEIGHTS_WITHOUT_PUBLISH: dict[str, float] = {
    "initializing": 0.02,
    "gdal_preprocess": 0.23,
    "gdal_raster_tile": 0.75,
}


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return dict(weights)
    return {stage: value / total for stage, value in weights.items()}


def build_stage_ranges(weights: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Convert normalized stage weights into cumulative percent ranges."""
    ranges: dict[str, tuple[float, float]] = {"queued": (0.0, 0.0)}
    cumulative = 0.0
    for stage, weight in weights.items():
        if weight <= 0:
            continue
        start = round(cumulative * 100.0, 2)
        cumulative += weight
        end = round(cumulative * 100.0, 2)
        ranges[stage] = (start, end)
    ranges["done"] = (100.0, 100.0)
    ranges["failed"] = (0.0, 100.0)
    return ranges


def durations_to_ratios(durations: dict[str, float], stages: tuple[str, ...]) -> dict[str, float]:
    """Convert raw stage durations (seconds) into normalized ratios."""
    positive = {stage: durations[stage] for stage in stages if durations.get(stage, 0) > 0}
    total = sum(positive.values())
    if total <= 0:
        return {}
    return {stage: elapsed / total for stage, elapsed in positive.items()}


def merge_weight_ema(
    previous: dict[str, float],
    observed: dict[str, float],
    *,
    alpha: float,
) -> dict[str, float]:
    """Blend observed job ratios into stored weights with exponential moving average."""
    merged: dict[str, float] = dict(previous)
    for stage, ratio in observed.items():
        if stage in merged:
            merged[stage] = (1.0 - alpha) * merged[stage] + alpha * ratio
        else:
            merged[stage] = ratio
    return normalize_weights(merged)


def default_calibration_payload() -> dict[str, Any]:
    return {
        "with_publish": {
            "sample_count": 0,
            "weights": dict(DEFAULT_WEIGHTS_WITH_PUBLISH),
        },
        "without_publish": {
            "sample_count": 0,
            "weights": dict(DEFAULT_WEIGHTS_WITHOUT_PUBLISH),
        },
    }


class ProgressCalibrationStore:
    """Persist and serve calibrated stage weights from Redis."""

    def __init__(self, settings: Settings, redis_client) -> None:
        self._settings = settings
        self._redis = redis_client

    def _load(self) -> dict[str, Any]:
        raw = self._redis.get(CALIBRATION_KEY)
        if not raw:
            return default_calibration_payload()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt progress calibration payload; resetting to defaults")
            return default_calibration_payload()

    def _save(self, payload: dict[str, Any]) -> None:
        self._redis.set(CALIBRATION_KEY, json.dumps(payload))

    def get_stage_ranges(
        self,
        *,
        auto_publish: bool,
    ) -> tuple[dict[str, tuple[float, float]], str, int]:
        """Return stage ranges, weight source label, and sample count used."""
        payload = self._load()
        bucket_key = "with_publish" if auto_publish else "without_publish"
        bucket = payload.get(bucket_key) or {}
        sample_count = int(bucket.get("sample_count") or 0)
        default_weights = (
            DEFAULT_WEIGHTS_WITH_PUBLISH if auto_publish else DEFAULT_WEIGHTS_WITHOUT_PUBLISH
        )
        min_samples = self._settings.progress_calibration_min_samples

        if sample_count >= min_samples:
            weights = normalize_weights(bucket.get("weights") or default_weights)
            return build_stage_ranges(weights), "historical", sample_count

        return build_stage_ranges(default_weights), "default", sample_count

    def record_job_durations(
        self,
        durations: dict[str, float],
        *,
        auto_publish: bool,
    ) -> None:
        """Update calibration weights from a successfully completed job."""
        stages = TIMED_STAGES_WITH_PUBLISH if auto_publish else TIMED_STAGES_WITHOUT_PUBLISH
        observed = durations_to_ratios(durations, stages)
        if not observed:
            return

        payload = self._load()
        bucket_key = "with_publish" if auto_publish else "without_publish"
        bucket = payload.setdefault(bucket_key, {"sample_count": 0, "weights": {}})
        default_weights = (
            DEFAULT_WEIGHTS_WITH_PUBLISH if auto_publish else DEFAULT_WEIGHTS_WITHOUT_PUBLISH
        )
        previous_weights = normalize_weights(bucket.get("weights") or default_weights)
        alpha = self._settings.progress_calibration_ema_alpha

        bucket["weights"] = merge_weight_ema(previous_weights, observed, alpha=alpha)
        bucket["sample_count"] = int(bucket.get("sample_count") or 0) + 1
        payload[bucket_key] = bucket
        self._save(payload)

        logger.info(
            "Updated progress calibration (%s, n=%s): %s",
            bucket_key,
            bucket["sample_count"],
            bucket["weights"],
        )
