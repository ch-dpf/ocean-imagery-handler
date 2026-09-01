"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.ws import router as ws_router
from app.api_docs import OPENAPI_TAGS, SWAGGER_UI_PARAMETERS
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.tilesets_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="海洋影像处理服务",
    description=(
        "正射影像 GeoTIFF 预处理与 Cesium 影像瓦片切片服务。"
        "支持任务提交、进度查询、瓦片发布与工作区浏览。"
    ),
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
    swagger_ui_parameters=SWAGGER_UI_PARAMETERS,
)
app.include_router(router)
app.include_router(ws_router)


@app.get("/health", tags=["系统"], summary="健康检查", description="返回服务存活状态。")
async def health() -> dict[str, str]:
    return {"status": "ok"}
