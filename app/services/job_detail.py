"""Serialize Redis job metadata into API response models."""

from typing import Any

from app.schemas import ImageryJobDetail, JobProgress, JobStatus
from app.services.job_progress import compute_elapsed_seconds

_JOB_DETAIL_FIELDS = {
    "job_id",
    "status",
    "stage",
    "progress",
    "created_at",
    "completed_at",
    "elapsed_seconds",
    "input_path",
    "output_dir",
    "imagery_url",
    "tileset_name",
    "cesium_url_template",
    "published",
    "bounds_wgs84",
    "error",
}


def progress_from_store(data: dict[str, Any]) -> JobProgress | None:
    raw = data.get("progress")
    if not raw:
        return None
    return JobProgress.model_validate(raw)


def job_detail_from_store(data: dict[str, Any]) -> ImageryJobDetail:
    return ImageryJobDetail(
        job_id=data["job_id"],
        status=JobStatus(data["status"]),
        progress=progress_from_store(data),
        stage=data.get("stage"),
        created_at=data.get("created_at"),
        completed_at=data.get("completed_at"),
        elapsed_seconds=compute_elapsed_seconds(data),
        input_path=data.get("input_path"),
        output_dir=data.get("output_dir"),
        imagery_url=data.get("imagery_url"),
        tileset_name=data.get("tileset_name"),
        cesium_url_template=data.get("cesium_url_template"),
        published=bool(data.get("published")),
        error=data.get("error"),
        metadata={
            key: value
            for key, value in data.items()
            if key not in _JOB_DETAIL_FIELDS | {"request"}
        },
    )
