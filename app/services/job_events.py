"""Redis pub/sub channels for live job progress updates."""

from app.schemas import JobStatus

TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED.value, JobStatus.FAILED.value})


def job_events_channel(job_id: str) -> str:
    return f"imagery:job:events:{job_id}"
