"""Elapsed time computation tests."""

from datetime import UTC, datetime, timedelta

from app.services.job_progress import compute_elapsed_seconds


def test_compute_elapsed_seconds_running_job():
    created = datetime(2026, 8, 28, 7, 0, 0, tzinfo=UTC)
    now = created + timedelta(seconds=125)
    elapsed = compute_elapsed_seconds(
        {
            "created_at": created.isoformat(),
            "status": "tiling",
        },
        now=now,
    )
    assert elapsed == 125.0


def test_compute_elapsed_seconds_completed_uses_completed_at():
    created = datetime(2026, 8, 28, 7, 0, 0, tzinfo=UTC)
    completed = created + timedelta(seconds=90)
    elapsed = compute_elapsed_seconds(
        {
            "created_at": created.isoformat(),
            "completed_at": completed.isoformat(),
            "updated_at": (completed + timedelta(seconds=10)).isoformat(),
            "status": "completed",
        },
        now=completed + timedelta(hours=1),
    )
    assert elapsed == 90.0


def test_compute_elapsed_seconds_failed_falls_back_to_updated_at():
    created = datetime(2026, 8, 28, 7, 0, 0, tzinfo=UTC)
    updated = created + timedelta(seconds=42)
    elapsed = compute_elapsed_seconds(
        {
            "created_at": created.isoformat(),
            "updated_at": updated.isoformat(),
            "status": "failed",
        }
    )
    assert elapsed == 42.0


def test_compute_elapsed_seconds_missing_created_at():
    assert compute_elapsed_seconds({"status": "queued"}) is None
