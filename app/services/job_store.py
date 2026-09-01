"""Redis-backed job status store."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis

from app.config import Settings
from app.schemas import JobStatus
from app.services.job_events import job_events_channel

logger = logging.getLogger(__name__)


class CorruptJobDataError(RuntimeError):
    """Raised when Redis contains job metadata that is not valid JSON."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Corrupt job metadata for {job_id}")


class JobStore:
    def __init__(self, settings: Settings) -> None:
        self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        self._ttl = settings.job_ttl
        self._prefix = "imagery:job:"

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}{job_id}"

    def _publish(self, job_id: str, data: dict[str, Any]) -> None:
        try:
            self._redis.publish(job_events_channel(job_id), json.dumps(data))
        except redis.RedisError:
            logger.warning("Failed to publish job update for %s", job_id, exc_info=True)

    def create(self, job_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        data = {
            "job_id": job_id,
            "status": JobStatus.QUEUED.value,
            "stage": "queued",
            "created_at": now,
            "updated_at": now,
            "progress": {
                "percent": 0.0,
                "phase": "queued",
                "message": "Queued",
                "current_zoom": None,
                "min_zoom": None,
                "max_zoom": None,
            },
            **payload,
        }
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))
        self._publish(job_id, data)

    def update(self, job_id: str, **fields: Any) -> None:
        try:
            data = self.get(job_id)
        except CorruptJobDataError:
            self.overwrite(job_id, **fields)
            return
        if data is None:
            return
        data.update(fields)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))
        self._publish(job_id, data)

    def overwrite(self, job_id: str, **fields: Any) -> None:
        """Replace job metadata without reading the existing Redis value."""
        now = datetime.now(UTC).isoformat()
        data = {
            "job_id": job_id,
            "updated_at": now,
            **fields,
        }
        if "created_at" not in data:
            data["created_at"] = now
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))
        self._publish(job_id, data)

    def get(self, job_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._key(job_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Corrupt job metadata for %s", job_id)
            raise CorruptJobDataError(job_id) from exc

    @property
    def redis(self):
        return self._redis
