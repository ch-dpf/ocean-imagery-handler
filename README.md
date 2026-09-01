# Ocean Imagery Handler

正射影像 GeoTIFF 预处理与 Cesium 影像瓦片切片服务。基于 **FastAPI + Celery + Redis** 与自研 Python 栅格引擎（`tifffile` / `pyproj` / `Pillow`），通过 **Nginx** 发布影像瓦片供 Cesium 加载。

与姊妹项目 [ocean-terrain-handler](D:\workspace\ocean-terrain-handler) 架构对齐：地形服务负责 DEM → `CesiumTerrainProvider`，本服务负责正射影像 → `UrlTemplateImageryProvider`。

## 架构

```
客户端 → FastAPI → Redis 队列 → Celery Worker
                                    ├─ 预处理（重投影 / 概览图）
                                    ├─ 切片 → PNG/JPEG/WEBP 瓦片
                                    └─ 注册 tileset → nginx 发布

Cesium 客户端 → imagery-server :8102/imagery/{name}/{z}/{x}/{y}.png
浏览器预览 → :8102/preview/?tileset={name}
```

| 组件 | 职责 |
|------|------|
| API | 接收任务、文件上传、查询状态、发布管理 |
| Worker | 重投影预处理 + XYZ/TMS 切片 + 注册发布 |
| Redis | 任务队列与状态存储 |
| imagery-server | Nginx 静态瓦片 HTTP 服务 |
| 工作目录 | 输入影像、中间产物、瓦片输出、发布注册 |

## 处理流程

1. **校验** — 读取 GeoTIFF 元数据（范围、CRS、尺寸）
2. **投影** — 重采样到目标 CRS（默认 EPSG:3857，Cesium Web Mercator）
3. **概览图** — 写入 `.ovr` 金字塔，加速大范围低级别读取
4. **切片** — 生成 `{z}/{x}/{y}.png`（或 JPEG/WEBP）
5. **元数据** — 生成标准 `tile.json`（TileJSON 3.0：bounds、zoom、tiles URL）
6. **发布** — 注册到 `data/tilesets/imagery/{name}`，由 Nginx 对外服务

## 快速开始

### 前置条件

- Docker & Docker Compose
- Worker 镜像基于 `python:3.12-slim`（不再依赖 GDAL CLI / `libgdal`）

### 启动

```powershell
cd D:\workspace\ocean-imagery-handler
copy .env.example .env
docker compose up -d --build
```

服务地址：`http://localhost:8100`  
API 文档：`http://localhost:8100/docs`  
影像发布：`http://localhost:8102/imagery/{tileset_name}/{z}/{x}/{y}.png`  
影像预览：`http://localhost:8102/preview/?tileset={tileset_name}`  
预览页也支持 `?job={job_id}` 自动打开进度查询面板并轮询状态。

### 浏览器预览页

访问 `http://localhost:8102/preview/` 可使用内置 Cesium 预览与后台 API 集成：

| 菜单 | 功能 |
|------|------|
| **数据接入** | **本地上传** / **服务器文件**；可展开 **高级选项** 配置预处理、切片、发布参数 |
| **进度查询** | 按 `job_id` 查询状态、轮询进度，完成后可一键预览 |
| **瓦片发布** | 按 `job_id` 发布 / 下架 tileset |
| **图层管理** | 列出已发布 tileset 并切换加载 |

预览页通过 Nginx 同源代理 `/api/` 访问 FastAPI，无需额外 CORS 配置。


将正射 TIF 放入 `./data/` 后：

```bash
curl -X POST http://localhost:8100/api/v1/imagery/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "input_path": "/data/workspace/ortho.tif",
    "preprocess": {
      "target_crs": "EPSG:3857",
      "build_overviews": true,
      "compress": "DEFLATE",
      "add_alpha": true
    },
    "tiling_options": {
      "profile": "mercator",
      "tile_format": "PNG",
      "start_zoom": 18,
      "end_zoom": 0,
      "thread_count": 4,
      "resume": true
    }
  }'
```

### 上传文件提交

```bash
curl -X POST http://localhost:8100/api/v1/imagery/jobs/upload \
  -F "file=@ortho.tif"
```

### 查询任务状态

```bash
curl http://localhost:8100/api/v1/imagery/jobs/{job_id}
```

任务状态：`queued` → `preprocessing` → `tiling` → `publishing` → `completed` / `failed`

输出目录：`./data/jobs/{job_id}/tiles/`  
发布 URL：`http://localhost:8102/imagery/{job_id}/`（需手动发布，或开启 `auto_publish`）

### 发布 / 下架

任务仍在 Redis 中时：

```bash
curl -X POST http://localhost:8100/api/v1/imagery/jobs/{job_id}/publish \
  -H "Content-Type: application/json" \
  -d '{"tileset_name": "optional-name"}'

curl -X DELETE http://localhost:8100/api/v1/imagery/jobs/{job_id}/publish
```

Redis 中 `job_id` 已过期、但磁盘瓦片仍在时，可任选其一：

```bash
# 仍可用原接口：自动从 jobs/{job_id}/tiles/ 发布
curl -X POST http://localhost:8100/api/v1/imagery/jobs/{job_id}/publish

# 或不依赖 job 元数据的磁盘发布
curl -X POST http://localhost:8100/api/v1/imagery/tilesets/publish \
  -H "Content-Type: application/json" \
  -d '{"job_id": "{job_id}", "tileset_name": "optional-name"}'

# 按名称下架（不依赖 Redis）
curl -X DELETE http://localhost:8100/api/v1/imagery/tilesets/{tileset_name}
```

### Cesium 客户端加载

```javascript
const response = await fetch("http://localhost:8102/imagery/{job_id}/tile.json");
const meta = await response.json();

let url = meta.tiles[0];
if (meta.scheme === "tms") {
  url = url.replace("{y}", "{reverseY}");
}

const imageryProvider = new Cesium.UrlTemplateImageryProvider({
  url,
  tilingScheme: new Cesium.WebMercatorTilingScheme(),
  minimumLevel: meta.minzoom,
  maximumLevel: meta.maxzoom,
  rectangle: Cesium.Rectangle.fromDegrees(
    meta.bounds[0], meta.bounds[1], meta.bounds[2], meta.bounds[3]
  ),
});
viewer.imageryLayers.addImageryProvider(imageryProvider);
```

与地形叠加（需 ocean-terrain-handler）：

```javascript
viewer.terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:8081/tilesets/{terrain_job_id}"
);
viewer.imageryLayers.addImageryProvider(imageryProvider);
```

## API 参数

### 预处理 `preprocess`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `target_crs` | string | `EPSG:3857` | 目标坐标系 |
| `build_overviews` | bool | `true` | 构建概览图 |
| `block_size` | int | `256` | GeoTIFF 块大小（须为 16 的倍数） |
| `compress` | string | `DEFLATE` | 压缩方式：DEFLATE / LZW / JPEG（JPEG 不支持透明） |
| `jpeg_quality` | int | `85` | JPEG 质量（仅 compress=JPEG） |
| `add_alpha` | bool | `true` | 添加 Alpha，使影像外区域透明 |
| `white_as_transparent` | bool | `true` | 将纯白 RGB(255,255,255) 填充视为透明 |
| `near_white` | int | `0` | 预留容差（当前未使用，仅精确白色） |

### 切片 `tiling_options`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `profile` | string | `mercator` | `mercator` → WebMercatorQuad；`geodetic` → WorldCRS84Quad；`raster` |
| `tile_format` | string | `PNG` | `PNG` / `JPEG` / `WEBP` |
| `tile_size` | int | `256` | 瓦片像素尺寸 |
| `start_zoom` | int | 自动 | 最大 zoom（最精细） |
| `end_zoom` | int | `0` | 最小 zoom |
| `resampling_method` | string | `bilinear` | 切片重采样（`antialias` 映射为 `lanczos`） |
| `thread_count` | int | `TILING_THREAD_COUNT` | 并行任务数（`-j`）；请求省略时用环境变量 |
| `resume` | bool | `TILING_RESUME` | 断点续切；请求省略时用环境变量 |
| `tile_scheme` | string | `xyz` | `xyz` 或 `tms` |

### 发布 `publish`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `auto_publish` | bool | `AUTO_PUBLISH` | 切片完成后自动发布 |
| `tileset_name` | string | job_id | 发布名称 |

## 本地开发

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e ".[dev]"

# 启动 Redis（端口 6380，避免与 ocean-terrain-handler 的 6379 冲突）
docker run -p 6380:6379 redis:7-alpine

copy .env.example .env
# 修改 REDIS_URL 为 redis://localhost:6380/0

# 终端 1
uvicorn app.main:app --reload --port 8100

# 终端 2
celery -A app.worker.celery_app worker --loglevel=info
```

本地 Worker 需 Python 3.11+，并安装 `requirements.txt`（含 `numpy`、`pillow`、`tifffile`、`imagecodecs`、`pyproj`）。输入须为带地理参考的 **GeoTIFF**。

## 项目结构

```
ocean-imagery-handler/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── schemas.py
│   ├── api/routes.py
│   ├── services/
│   │   ├── preprocessor.py    # 重投影预处理
│   │   ├── tiler_runner.py    # XYZ/TMS 切片
│   │   ├── raster/            # 自研 GeoTIFF / warp / tile 引擎
│   │   ├── tile_json.py       # tile.json (TileJSON 3.0)
│   │   ├── tile_publisher.py  # 瓦片发布注册
│   │   └── job_store.py
│   └── worker/
│       ├── celery_app.py
│       └── tasks.py
├── docker/
│   └── nginx.conf
├── scripts/preview/             # Cesium 预览页 (index.html, app.js, styles.css)
├── tests/
├── docker-compose.yml
└── requirements.txt
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接 |
| `WORKSPACE_DIR` | `/data/workspace` | 工作目录 |
| `GDAL_CACHEMAX` | `512` | 读取 GeoTIFF 时的窗口缓存 (MB)，环境变量名保持兼容 |
| `JOB_TTL` | `604800` | 任务状态保留 (秒) |
| `IMAGERY_SERVER_PUBLIC_URL` | `http://localhost:8102` | 对外 URL |
| `IMAGERY_BASE_PATH` | `/imagery` | URL 前缀 |
| `AUTO_PUBLISH` | `false` | 自动发布 |
| `TILING_THREAD_COUNT` | — | 默认并行切片线程数（请求未指定 `thread_count` 时生效） |
| `TILING_RESUME` | `false` | 默认是否断点续切（请求未指定 `resume` 时生效） |

## 注意事项

- 输入应为 RGB/RGBA 正射影像，不是高程 DEM
- 默认 Web Mercator（EPSG:3857）+ `mercator` profile，与 Cesium `WebMercatorTilingScheme` 匹配
- 大文件建议设置 `start_zoom` / `end_zoom` 控制级别，避免磁盘爆满
- 发布通过符号链接注册瓦片，Worker 需有创建 symlink 权限
- Redis 中 job 元数据会随 `JOB_TTL` 过期；磁盘瓦片仍可通过 `/jobs/{id}/publish` 或 `/tilesets/publish` 发布
- `imagery-server` 挂载完整 `./data` 目录，以便 symlink 解析到 `jobs/` 下的瓦片
- Docker 镜像基于 `python:3.12-slim`，栅格处理为自研 Python 实现，不调用 GDAL 命令行，也不链接 `libgdal`

## License

Apache-2.0
