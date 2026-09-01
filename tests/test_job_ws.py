"""WebSocket job progress tests."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app


def _sample_job(**overrides: object) -> dict:
    data = {
        "job_id": "job-1",
        "status": "running",
        "stage": "gdal_preprocess",
        "progress": {
            "percent": 12.5,
            "phase": "gdal_preprocess",
            "message": "Working",
        },
    }
    data.update(overrides)
    return data


def test_ws_sends_initial_snapshot_and_subscribes() -> None:
    with patch("app.api.ws.JobStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.get.return_value = _sample_job()

        with patch(
            "app.api.ws._listen_for_job_updates",
            new_callable=AsyncMock,
        ) as mock_listen:
            with TestClient(app) as client:
                with client.websocket_connect("/api/v1/imagery/jobs/job-1/ws") as ws:
                    payload = ws.receive_json()

            assert payload["job_id"] == "job-1"
            assert payload["status"] == "running"
            assert payload["progress"]["percent"] == 12.5
            mock_listen.assert_awaited_once()


def test_ws_closes_for_completed_job_without_subscribing() -> None:
    with patch("app.api.ws.JobStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.get.return_value = _sample_job(status="completed", stage="done")

        with patch(
            "app.api.ws._listen_for_job_updates",
            new_callable=AsyncMock,
        ) as mock_listen:
            with TestClient(app) as client:
                with client.websocket_connect("/api/v1/imagery/jobs/job-1/ws") as ws:
                    payload = ws.receive_json()

            assert payload["status"] == "completed"
            mock_listen.assert_not_called()


def test_ws_rejects_missing_job() -> None:
    with patch("app.api.ws.JobStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.get.return_value = None

        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/imagery/jobs/missing/ws") as ws:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                assert exc_info.value.code == 1008
