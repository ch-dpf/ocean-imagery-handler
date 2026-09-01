"""Job store tests."""

import json

import pytest

from app.services.job_store import CorruptJobDataError, JobStore


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self._data[key] = value

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


class _FakeSettings:
    redis_url = "redis://localhost:6379/0"
    job_ttl = 3600


def _store(fake_redis: _FakeRedis) -> JobStore:
    store = JobStore(_FakeSettings())  # type: ignore[arg-type]
    store._redis = fake_redis  # type: ignore[assignment]
    return store


def test_get_raises_for_corrupt_metadata() -> None:
    fake = _FakeRedis()
    fake.setex("imagery:job:bad-job", 3600, "{job_id: bad-job, status: completed}")
    store = _store(fake)

    with pytest.raises(CorruptJobDataError):
        store.get("bad-job")


def test_update_recovers_from_corrupt_metadata() -> None:
    fake = _FakeRedis()
    fake.setex("imagery:job:bad-job", 3600, "{job_id: bad-job, status: completed}")
    store = _store(fake)

    store.update("bad-job", published=False, status="completed")

    data = store.get("bad-job")
    assert data is not None
    assert data["job_id"] == "bad-job"
    assert data["published"] is False
    assert data["status"] == "completed"
    json.loads(fake.get("imagery:job:bad-job"))


def test_update_publishes_job_event() -> None:
    fake = _FakeRedis()
    store = _store(fake)
    store.create("job-1", {"input_path": "/data/test.tif"})
    fake.published.clear()

    store.update("job-1", status="running", stage="gdal_preprocess")

    assert len(fake.published) == 1
    channel, message = fake.published[0]
    assert channel == "imagery:job:events:job-1"
    payload = json.loads(message)
    assert payload["status"] == "running"
    assert payload["stage"] == "gdal_preprocess"
