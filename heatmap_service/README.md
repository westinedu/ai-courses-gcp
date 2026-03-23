# Heatmap Service (Cloud Run)

`heatmap_service` 是面向多市场（先 `HK`）的 Heatmap 数据服务。

- 读取市场成分股配置（本地 `config/markets.json`，可选 GCS 覆盖）。
- 拉取行情并生成 Heatmap 快照。
- 内存缓存（默认 300 秒）+ 可选 GCS 持久化（`latest.json` + 可选历史）。
- `GET` 只读最新快照：`L1 内存缓存 -> L2 GCS`，不主动触发实时重算。
- 提供 Scheduler 可调用的刷新接口（`POST /refresh`），由 GCP 侧频控产数。

## 当前支持

- `HK`（港股）
- 结构已预留扩展 `KS/US/...`，后续只需在配置中增加市场与成分股。

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
  - 强制重算多个市场（建议仅供 Scheduler 或受控调用）。
  - 可选 Header: `x-heatmap-token: <HEATMAP_CRON_TOKEN>`

## 部署 Cloud Run

```bash
cd GCP/heatmap_service
./deploy_heatmap_service.sh
```

## Cloud Scheduler 建议

基于 `yfinance` 建议 Scheduler 每 5 分钟触发（可按交易时段单独配置）：

- Method: `POST`
- URL: `https://<heatmap-service-url>/v1/heatmap/refresh_all`
- Header: `x-heatmap-token: <same token>`（如果你配置了 `HEATMAP_CRON_TOKEN`）
- Body: `{ "markets": ["hk"] }`

这样可实现“GCP 端受控近实时产数”，前端仅拉取最新快照，不对实时重算施压。

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
SCHEDULE="*/5 * * * 1-5" \
TIME_ZONE="Asia/Hong_Kong" \
MARKETS_CSV="hk" \
HEATMAP_CRON_TOKEN="<same token>" \
./configure_heatmap_scheduler.sh
```
