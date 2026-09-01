"""Job event channel tests."""

from app.services.job_events import TERMINAL_JOB_STATUSES, job_events_channel


def test_job_events_channel() -> None:
    assert job_events_channel("abc-123") == "imagery:job:events:abc-123"


def test_terminal_job_statuses() -> None:
    assert "completed" in TERMINAL_JOB_STATUSES
    assert "failed" in TERMINAL_JOB_STATUSES
    assert "running" not in TERMINAL_JOB_STATUSES
