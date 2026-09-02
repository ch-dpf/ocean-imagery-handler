# Ocean Imagery Handler — 裸机部署手册

本文档评估当前分支的裸机（非 Docker）部署能力，并给出 Linux 生产环境与 Windows 开发/测试环境的安装步骤。

---

## 1. 评估结论

| 维度 | 结论 |
|------|------|
| **是否支持裸机部署** | **支持**。应用为标准 Python 栈（FastAPI + Celery + Redis），无 Docker 运行时依赖。 |
| **官方主推方式** | Docker Compose（见根目录 `README.md`）。裸机需自行编排进程与 Nginx。 |
| **开箱即用程度** | **中等**。README「本地开发」章节已覆盖最小裸机流程；本仓库新增 `deploy/` 下的 Nginx、systemd、环境变量模板，但**无一键安装脚本**。 |
| **系统依赖** | 轻量：Python 3.11+、Redis、Nginx（瓦片 HTTP 与预览页）；Worker 镜像仅需 `libgomp1`、`libglib2.0-0`（OpenCV / 数值库），**不依赖 GDAL CLI 或 libgdal**。 |
| **关键约束** | 瓦片发布通过**符号链接**注册；Worker 进程必须有创建 symlink 的权限。Nginx 需能解析指向 `jobs/` 的相对 symlink。 |

### 与 Docker 部署的差异

| 项目 | Docker Compose | 裸机 |
|------|----------------|------|
| API 端口 | 宿主机 `8100` → 容器 `8000` | 直接监听 `8100`（可改） |
| Redis | 容器内 `6379`，映射宿主机 `6380` | 本机 Redis，通常 `6379` |
| 数据目录 | `./data` 挂载为 `/data/workspace` | 自定义路径，如 `/var/lib/ocean-imagery-handler/data` |
| Nginx upstream | Docker DNS `api:8000` | `127.0.0.1:8100` |
| 进程管理 | Compose 自动重启 | 需 systemd / supervisord 等 |

### 当前分支未包含的内容

- 自动化安装脚本（Ansible / shell installer）
- Redis / Nginx 的 systemd 单元（仅提供 API 与 Worker）
- TLS / 反向代理到 443 的完整示例
- 集群多 Worker 节点的编排说明（可按 Celery 标准水平扩展）

---

## 2. 架构与端口

```
客户端 / Cesium
    │
    ├─► Nginx :8102  ── /imagery/*     静态瓦片
    │              ── /preview/*     Cesium 预览页
    │              ── /api/*         反向代理 → FastAPI :8100
    │
    └─► FastAPI :8100  任务提交、状态、发布 API

Celery Worker ──► Redis :6379  任务队列与 job 元数据
Worker 读写 ──► WORKSPACE_DIR（jobs / uploads / tilesets）
```

| 服务 | 默认端口 | 说明 |
|------|----------|------|
| FastAPI | 8100 | REST + WebSocket |
| Nginx（imagery-server） | 8102 | 瓦片、预览、API 同源代理 |
| Redis | 6379（裸机）/ 6380（Docker 映射） | Celery broker + job store |

---

## 3. 前置条件

### 3.1 硬件与 OS

- **推荐**：Linux x86_64（Ubuntu 22.04+ / Debian 12+ / RHEL 8+）
- **内存**：≥ 8 GB（大 GeoTIFF 预处理与切片建议 16 GB+）
- **磁盘**：输入影像 + 中间产物 + 瓦片；按 zoom 级别预留足够空间
- **CPU**：Worker `--concurrency` 与 `TILING_THREAD_COUNT` 可按核数调整

### 3.2 软件版本

| 组件 | 版本要求 |
|------|----------|
| Python | ≥ 3.11（`pyproject.toml`）；生产建议 3.12 |
| Redis | ≥ 7 |
| Nginx | ≥ 1.18 |

### 3.3 Linux 系统包（Debian/Ubuntu 示例）

Worker 依赖 OpenCV headless 与数值库，需安装：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3.12 python3.12-venv python3-pip \
  redis-server nginx \
  libgomp1 libglib2.0-0
```

---

## 4. Linux 生产部署

以下示例路径：

- 代码：`/opt/ocean-imagery-handler`
- 数据：`/var/lib/ocean-imagery-handler/data`
- 环境文件：`/etc/ocean-imagery-handler/env`
- 运行用户：`ocean-imagery`

### 4.1 创建用户与目录

```bash
sudo useradd --system --home /opt/ocean-imagery-handler --shell /usr/sbin/nologin ocean-imagery

sudo mkdir -p /opt/ocean-imagery-handler
sudo mkdir -p /var/lib/ocean-imagery-handler/data
sudo mkdir -p /etc/ocean-imagery-handler

sudo chown -R ocean-imagery:ocean-imagery /opt/ocean-imagery-handler
sudo chown -R ocean-imagery:ocean-imagery /var/lib/ocean-imagery-handler
```

### 4.2 部署代码

```bash
sudo -u ocean-imagery git clone <your-repo-url> /opt/ocean-imagery-handler
cd /opt/ocean-imagery-handler
sudo -u ocean-imagery git checkout master-zy   # 或目标分支

sudo -u ocean-imagery python3.12 -m venv .venv
sudo -u ocean-imagery .venv/bin/pip install -U pip
sudo -u ocean-imagery .venv/bin/pip install -r requirements.txt
sudo -u ocean-imagery .venv/bin/pip install -e .
```

### 4.3 配置环境变量

```bash
sudo cp deploy/env.production.example /etc/ocean-imagery-handler/env
sudo nano /etc/ocean-imagery-handler/env
```

**必须修改**：

- `IMAGERY_SERVER_PUBLIC_URL` — 客户端访问瓦片的外网 URL，如 `http://192.168.1.10:8102`
- `WORKSPACE_DIR` — 与数据目录一致
- `REDIS_URL` / `CELERY_*` — 指向本机 Redis

应用启动时会自动创建：

```
{WORKSPACE_DIR}/jobs/
{WORKSPACE_DIR}/uploads/
{WORKSPACE_DIR}/tilesets/imagery/
```

### 4.4 启动 Redis

```bash
sudo systemctl enable --now redis-server
redis-cli ping   # 应返回 PONG
```

若本机已有 Redis 且端口冲突，可修改 `/etc/redis/redis.conf` 或环境变量中的 URL。

### 4.4 安装 systemd 服务

```bash
sudo cp deploy/systemd/ocean-imagery-api.service /etc/systemd/system/
sudo cp deploy/systemd/ocean-imagery-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocean-imagery-api ocean-imagery-worker
```

查看状态：

```bash
sudo systemctl status ocean-imagery-api ocean-imagery-worker
journalctl -u ocean-imagery-api -f
journalctl -u ocean-imagery-worker -f
```

Worker 并发可在 `ocean-imagery-worker.service` 中调整 `--concurrency=N`。

### 4.5 配置 Nginx

1. 编辑 `deploy/nginx-baremetal.conf`，确认以下路径与安装一致：
   - `/var/lib/ocean-imagery-handler/data/tilesets/imagery/`（瓦片 alias）
   - `/opt/ocean-imagery-handler/scripts/preview/`（预览页）
   - `upstream` 中的 API 地址（默认 `127.0.0.1:8100`）

2. 安装站点配置：

```bash
sudo cp deploy/nginx-baremetal.conf /etc/nginx/sites-available/ocean-imagery-handler
sudo ln -sf /etc/nginx/sites-available/ocean-imagery-handler /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

**重要**：Nginx 的 `/imagery/` 必须 alias 到 `tilesets/imagery/`，且该目录下的 symlink 能相对解析到 `../../jobs/{job_id}/tiles/`。不要将 alias 指到单独的 jobs 子目录。

### 4.6 防火墙（可选）

```bash
sudo ufw allow 8100/tcp   # API（若需直连）
sudo ufw allow 8102/tcp   # 瓦片 + 预览 + 同源 API 代理
```

生产环境建议仅暴露 `8102`，API 经 Nginx 代理访问。

---

## 5. 验证部署

### 5.1 健康检查

```bash
curl -s http://127.0.0.1:8100/health
curl -s http://127.0.0.1:8102/health
```

### 5.2 提交测试任务

将 GeoTIFF 放入数据目录，例如 `/var/lib/ocean-imagery-handler/data/ortho.tif`：

```bash
curl -X POST http://127.0.0.1:8100/api/v1/imagery/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "input_path": "/var/lib/ocean-imagery-handler/data/ortho.tif",
    "preprocess": {"target_crs": "EPSG:3857", "build_overviews": true},
    "tiling_options": {"profile": "mercator", "end_zoom": 0, "thread_count": 4}
  }'
```

查询状态：

```bash
curl http://127.0.0.1:8100/api/v1/imagery/jobs/{job_id}
```

### 5.3 发布与访问瓦片

```bash
curl -X POST http://127.0.0.1:8100/api/v1/imagery/jobs/{job_id}/publish \
  -H "Content-Type: application/json" \
  -d '{"tileset_name": "demo"}'

curl -s http://127.0.0.1:8102/imagery/demo/tile.json | head
```

浏览器打开：`http://<host>:8102/preview/?tileset=demo`

### 5.4 符号链接权限

若发布失败并提示 symlink 错误：

```bash
# 确认 Worker 用户对 WORKSPACE_DIR 有写权限
sudo -u ocean-imagery ln -sfn /var/lib/ocean-imagery-handler/data/jobs/test/tiles \
  /var/lib/ocean-imagery-handler/data/tilesets/imagery/test-link
```

---

## 6. Windows 裸机部署（开发 / 测试）

README 已描述最小流程，要点如下。

### 6.1 依赖

- Python 3.11+（从 [python.org](https://www.python.org/) 安装）
- Redis：可用 `docker run -p 6380:6379 redis:7-alpine`，或 [Memurai](https://www.memurai.com/) / WSL2 内 Redis
- Nginx for Windows（可选；也可仅使用 API 端口 8100 调试，瓦片 URL 需单独配置）

### 6.2 启动步骤

```powershell
cd D:\workspace\ocean-imagery-handler
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

copy .env.example .env
# 编辑 .env：REDIS_URL=redis://localhost:6380/0
# WORKSPACE_DIR 设为绝对路径，如 D:\ocean-imagery-data

# 终端 1
uvicorn app.main:app --reload --port 8100

# 终端 2
celery -A app.worker.celery_app worker --loglevel=info
```

### 6.3 Windows 符号链接

瓦片发布需要创建目录 symlink。任选其一：

- 启用 **开发者模式**（设置 → 隐私和安全性 → 开发者模式）
- 或以**管理员**身份运行 Celery Worker

否则 `publish` 步骤会报 `PublishError: Failed to create symlink`。

### 6.4 推荐替代

在 Windows 上用于生产时，更推荐使用 **WSL2 + 本文第 4 节 Linux 流程**，或直接使用 **Docker Compose**。

---

## 7. 运维说明

### 7.1 日志

| 组件 | 日志位置 |
|------|----------|
| API | `journalctl -u ocean-imagery-api` |
| Worker | `journalctl -u ocean-imagery-worker` |
| Nginx | `/var/log/nginx/error.log` |
| Redis | `/var/log/redis/redis-server.log` |

### 7.2 升级

```bash
cd /opt/ocean-imagery-handler
sudo -u ocean-imagery git pull
sudo -u ocean-imagery .venv/bin/pip install -r requirements.txt
sudo systemctl restart ocean-imagery-api ocean-imagery-worker
```

### 7.3 扩容 Worker

在同一 Redis 与 `WORKSPACE_DIR`（共享存储，如 NFS）下，可在多台机器启动额外 Worker：

```bash
celery -A app.worker.celery_app worker --loglevel=info --concurrency=4 -n worker2@%h
```

API 与 Nginx 通常单实例即可；多 API 实例需负载均衡并保证上传文件落在共享存储。

### 7.4 磁盘与任务保留

- 瓦片输出：`{WORKSPACE_DIR}/jobs/{job_id}/tiles/`
- Redis 中 job 元数据默认保留 7 天（`JOB_TTL=604800`）；过期后仍可用磁盘发布 API：
  - `POST /api/v1/imagery/tilesets/publish`
  - `POST /api/v1/imagery/jobs/{job_id}/publish`

### 7.5 与 ocean-terrain-handler 联调

地形服务默认 `8081`，影像瓦片 `8102`。Cesium 同时加载时需保证两者 `IMAGERY_SERVER_PUBLIC_URL` / 地形 public URL 对浏览器可达（CORS 由 Nginx 瓦片 location 处理）。

---

## 8. 故障排查

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 任务一直 `queued` | Worker 未启动或 Redis 不可达 | 检查 `systemctl status ocean-imagery-worker`、`redis-cli ping` |
| 发布失败 symlink | 权限不足 | 第 4.6 / 5.4 节 |
| 瓦片 404 | 未发布或 Nginx alias 路径错误 | 检查 `tilesets/imagery/` 下链接与 nginx 配置 |
| 预览页 API 失败 | Nginx 未代理 `/api/` 或 API 未监听 8100 | 检查 upstream 与防火墙 |
| 预处理 OOM | 影像过大 | 增大内存或降低并发、限制 zoom 范围 |
| `input_path` 找不到 | Worker 与 API 的 `WORKSPACE_DIR` 不一致 | 统一环境文件 |

---

## 9. 配置文件索引

| 文件 | 用途 |
|------|------|
| `.env.example` | 开发 / Docker 环境变量模板 |
| `deploy/env.production.example` | 裸机生产环境变量模板 |
| `deploy/nginx-baremetal.conf` | 裸机 Nginx 站点配置 |
| `deploy/systemd/ocean-imagery-api.service` | API systemd 单元 |
| `deploy/systemd/ocean-imagery-worker.service` | Worker systemd 单元 |
| `docker-compose.yml` | Docker 一键部署（对照参考） |

---

## 10. 快速对照：Docker vs 裸机命令

| 操作 | Docker | 裸机 |
|------|--------|------|
| 启动全部 | `docker compose up -d --build` | `systemctl start redis ocean-imagery-api ocean-imagery-worker nginx` |
| API 文档 | http://localhost:8100/docs | 同左 |
| 瓦片 URL | http://localhost:8102/imagery/... | 将 host 换为服务器 IP/域名 |
| 查看 Worker 日志 | `docker compose logs -f worker` | `journalctl -u ocean-imagery-worker -f` |

---

**结论**：当前分支在功能上**完全支持裸机部署**；运维侧需自行安装 Redis、Nginx、Python 虚拟环境，并使用本目录 `deploy/` 内模板完成进程托管。若追求最少运维成本，仍建议优先使用 Docker Compose。
