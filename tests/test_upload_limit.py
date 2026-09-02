"""Upload size limit helpers and API rejection."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes import (
    _reject_if_upload_too_large,
    upload_limit_exceeded_detail,
)
from app.main import app


def test_upload_limit_message_mentions_server_file() -> None:
    detail = upload_limit_exceeded_detail(2 * 1024**3)
    assert "2GB" in detail
    assert "服务器文件" in detail


def test_reject_if_upload_too_large_raises_413() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _reject_if_upload_too_large(2 * 1024**3 + 1, 2 * 1024**3)
    assert exc_info.value.status_code == 413
    assert "服务器文件" in str(exc_info.value.detail)


def test_reject_if_upload_too_large_allows_equal_limit() -> None:
    _reject_if_upload_too_large(2 * 1024**3, 2 * 1024**3)
    _reject_if_upload_too_large(None, 2 * 1024**3)


def test_upload_endpoint_rejects_when_stream_exceeds_limit(tmp_path) -> None:
    settings = MagicMock()
    settings.max_upload_bytes = 8
    settings.uploads_dir = tmp_path

    with patch("app.api.routes.get_settings", return_value=settings):
        with patch("app.api.routes.create_job_from_upload") as mock_create:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/imagery/jobs/upload",
                    files={
                        "file": (
                            "big.tif",
                            b"0123456789ABCDEF",
                            "image/tiff",
                        )
                    },
                )

    assert response.status_code == 413
    assert "服务器文件" in response.json()["detail"]
    mock_create.assert_not_called()
    assert list(tmp_path.iterdir()) == []
