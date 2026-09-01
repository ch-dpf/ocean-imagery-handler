"""任务进度 WebSocket 路由。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.services.job_detail import job_detail_from_store
from app.services.job_events import TERMINAL_JOB_STATUSES, job_events_channel
from app.services.job_store import CorruptJobDataError, JobStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/imagery", tags=["影像服务 · WebSocket"])


async def _send_job_detail(websocket: WebSocket, data: dict[str, Any]) -> None:
    detail = job_detail_from_store(data)
    await websocket.send_json(detail.model_dump(mode="json"))


async def _listen_for_job_updates(
    websocket: WebSocket,
    job_id: str,
    *,
    redis_url: str,
) -> None:
    channel = job_events_channel(job_id)
    redis = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe(channel)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                await asyncio.sleep(0)
                continue
            if message.get("type") != "message":
                continue

            payload = json.loads(message["data"])
            await _send_job_detail(websocket, payload)
            if payload.get("status") in TERMINAL_JOB_STATUSES:
                break
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await redis.aclose()


@router.websocket("/jobs/{job_id}/ws")
async def job_progress_websocket(websocket: WebSocket, job_id: str) -> None:
    """实时推送任务进度。

    先发送与 GET /jobs/{job_id} 相同结构的初始快照，随后持续推送更新，
    直至任务进入终态或客户端断开连接。
    """
    await websocket.accept()
    store = JobStore(get_settings())

    try:
        data = store.get(job_id)
    except CorruptJobDataError:
        await websocket.close(code=1011, reason="Corrupt job metadata")
        return

    if data is None:
        await websocket.close(code=1008, reason="Job not found")
        return

    await _send_job_detail(websocket, data)
    if data.get("status") in TERMINAL_JOB_STATUSES:
        await websocket.close()
        return

    try:
        await _listen_for_job_updates(
            websocket,
            job_id,
            redis_url=get_settings().redis_url,
        )
    except WebSocketDisconnect:
        logger.debug("Job progress websocket disconnected for %s", job_id)
    except Exception:
        logger.exception("Job progress websocket failed for %s", job_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Internal error")
        return

    with contextlib.suppress(Exception):
        await websocket.close()
