# Heatmap Service (Cloud Run)

`heatmap_service` 是面向多市场的 Heatmap 数据服务。

- 读取市场成分股配置（本地 `config/markets.json`，可选 GCS 覆盖）。
- 拉取行情并生成 Heatmap 快照。
- 内存缓存（默认 300 秒）+ 可选 GCS 持久化（`latest.json` + 可选历史）。
- `GET` 只读最新快照：`L1 内存缓存 -> L2 GCS`，不主动触发实时重算。
- 提供 Scheduler 可调用的刷新接口（`POST /refresh`），由 GCP 侧频控产数。

## 当前支持

- `HK`（港股）
- `TW`（台湾）
- `JP`（日本）
- `KS`（韩国）

## 目录

```text
heatmap_service/
├── config/markets.json
├── main.py
├── requirements.txt
├── Dockerfile
├── deploy_heatmap_service.sh
├── configure_heatmap_scheduler.sh
└── README.md
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

打开：`http://localhost:8080/docs`

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `GCS_BUCKET_NAME` | 快照存储桶（为空则不写 GCS） | `""` |
| `HEATMAP_GCS_PREFIX` | GCS 快照前缀 | `heatmap/snapshots` |
| `HEATMAP_CACHE_TTL_SECONDS` | 内存缓存秒数 | `300` |
| `HEATMAP_QUOTE_TIMEOUT_SECONDS` | 单市场抓价软超时秒数（`0`=关闭） | `90` |
| `HEATMAP_WRITE_HISTORY` | 是否写历史快照（写入前先归档上一版 latest） | `1` |
| `HEATMAP_DEFAULT_MARKET` | 默认市场 | `hk` |
| `HEATMAP_MARKETS_CONFIG_BLOB` | 可选：GCS 配置路径 | `""` |
| `HEATMAP_CRON_TOKEN` | 可选：刷新接口鉴权 token | `""` |

## API

- `GET /health`
- `GET /v1/markets`
- `GET /v1/heatmap/{market}`
  - 只读路径：按 `L1 内存缓存 -> L2 GCS` 返回最新快照，不触发重算。
  - 快照现在会附带可选 `index` 摘要（例如 `HK` 的 `^HSI`），便于前端顶部指数卡和热图共用同一份产数。
- `POST /v1/heatmap/{market}/refresh`
  - 强制实时重算并更新缓存/GCS（建议仅供 Scheduler 或受控调用）。
  - 可选 Header: `x-heatmap-token: <HEATMAP_CRON_TOKEN>`
- `POST /v1/heatmap/refresh_all`
  - Body (optional): `{ "markets": ["hk"] }`
  - 默认是“受控刷新”：只会刷新当前处于交易时段且到达市场 cadence 的市场。
  - 当前内置 cadence / 窗口（`Asia/Hong_Kong`）：
    - `HK`: `09:30-16:10`，每 `10` 分钟
    - `TW`: `09:00-13:30`，每 `10` 分钟
    - `JP`: 窗口 `08:00-14:30`，固定槽位 `08:20/08:50/09:20/...`
    - `KS`: 窗口 `08:20-14:30`，固定槽位 `08:30/09:00/09:30/...`
  - 可选 Body: `{ "markets": ["hk", "tw"], "force": true }`
  - `force=true` 会绕过时间窗与 cadence 判断，恢复为原来的全量强制刷新行为。
  - 可选 Header: `x-heatmap-token: <HEATMAP_CRON_TOKEN>`

## 部署 Cloud Run

```bash
cd GCP/heatmap_service
./deploy_heatmap_service.sh
```

## Cloud Scheduler 建议

建议保留一个覆盖并集交易时段的 Scheduler 心跳，让服务端按市场时间窗与 cadence 决定是否真正刷新：

- Method: `POST`
- URL: `https://<heatmap-service-url>/v1/heatmap/refresh_all`
- Header: `x-heatmap-token: <same token>`（如果你配置了 `HEATMAP_CRON_TOKEN`）
- Body: `{ "markets": ["hk", "tw", "jp", "ks"] }`

这样可实现“GCP 端受控近实时产数”，前端仅拉取最新快照，不对实时重算施压，同时避免把四个市场无条件塞进一次长任务。

可直接使用独立脚本配置（与部署脚本解耦）：

```bash
cd GCP/heatmap_service
HEATMAP_CRON_TOKEN="<same token>" ./configure_heatmap_scheduler.sh
```

常见覆盖参数示例：

```bash
PROJECT_ID="your-project" \
REGION="us-central1" \
SCHEDULER_REGION="us-central1" \
JOB_NAME="heatmap-refresh-hk" \
SCHEDULE="*/10 8-16 * * 1-5" \
TIME_ZONE="Asia/Hong_Kong" \
MARKETS_CSV="hk,tw,jp,ks" \
HEATMAP_CRON_TOKEN="<same token>" \
./configure_heatmap_scheduler.sh
```

立即触发一次当前 Scheduler job（不改变服务端 cadence / 时间窗判断）：

```bash
cd GCP/heatmap_service
RUN_NOW="1"  ./configure_heatmap_scheduler.sh
```

直接调用 Heatmap service 并强制刷新，绕过受控刷新逻辑（等价于旧版 `refresh_all`）：

```bash
curl -X POST "https://<heatmap-service-url>/v1/heatmap/refresh_all" \
  -H "Content-Type: application/json" \
  -H "x-heatmap-token: <same token>" \
  -d '{"markets":["hk","tw","jp","ks"],"force":true}'
```

只强制刷新单个市场示例：

```bash
curl -X POST "https://<heatmap-service-url>/v1/heatmap/refresh_all" \
  -H "Content-Type: application/json" \
  -H "x-heatmap-token: <same token>" \
  -d '{"markets":["hk"],"force":true}'
```
