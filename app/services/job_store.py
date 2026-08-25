"""Redis-backed job status store."""

import json
from datetime import UTC, datetime
from typing import Any

import redis

from app.config import Settings
from app.schemas import JobStatus


class JobStore:
    def __init__(self, settings: Settings) -> None:
        self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        self._ttl = settings.job_ttl
        self._prefix = "imagery:job:"

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}{job_id}"

    def create(self, job_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        data = {
            "job_id": job_id,
            "status": JobStatus.QUEUED.value,
            "created_at": now,
            "updated_at": now,
            **payload,
        }
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))

    def update(self, job_id: str, **fields: Any) -> None:
        data = self.get(job_id)
        if data is None:
            return
        data.update(fields)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._redis.setex(self._key(job_id), self._ttl, json.dumps(data))

    def get(self, job_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._key(job_id))
        if raw is None:
            return None
        return json.loads(raw)
