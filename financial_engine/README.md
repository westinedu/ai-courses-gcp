# Financial Data Engine - 美股财报数据服务

`financial-engine` 是一个基于 **FastAPI** 构建的财报数据微服务，负责：

- 按需抓取与更新 **yfinance** 的公司财务报表（年/季/现金流/资产负债表/earnings）。
- **L1/L2 缓存读取链路**：L1（实例内存）→ L2（GCS/本地 fallback）→ upstream（yfinance）。
- 仅在“财报日已到且尚未刷新”时才触发 upstream 刷新（财报日来自 trading service 的 next earnings endpoint）。
- 生成**面向 AI 的上下文**（AI context），每天为每只股票落一份 `txt`，并维护**当日清单**供图卡/QA 调用。
- 将原始 JSON 与 AI context 写入 **Google Cloud Storage (GCS)**。
- 提供健康检查与批量调度接口，适合部署到 **Google Cloud Run**。

---

## 目录结构

```
financial_engine/
├── Dockerfile
├── requirements.txt
├── deploy_financial_engine.sh        # Cloud Run 一键部署脚本（可按需修改）
├── main.py                           # FastAPI 主程序
└── data/                             # 示例铺底数据（本地开发用）
```

---

## 运行前置

- Python 3.11+
- 已安装 [Google Cloud SDK](https://cloud.google.com/sdk)
- 已在目标项目启用：Cloud Run、Cloud Build、Artifact Registry、Cloud Storage
- Cloud Run 服务账号至少具备：`Storage Object Admin`

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `GCS_BUCKET_NAME` | 用于保存财报 JSON 与 AI context 的 GCS 桶 | `financial-data-engine-bucket` |
| `ENGINE_TZ` | 时区（用于 Context 日期、调度等） | `America/Los_Angeles` |
| `TRADING_DATA_ENGINE_URL` | Trading 服务地址（用于查询 next earnings day） | `""` |
| `FINANCIAL_L1_HIT_TTL_SECONDS` | L1 命中缓存 TTL（秒） | `600` |
| `FINANCIAL_L1_MISS_TTL_SECONDS` | L1 空结果 TTL（秒） | `120` |
| `FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS` | 若拿不到 earnings day，超过该天数自动兜底刷新 | `3` |
| `REPORT_SOURCE_PREFIX` | 财报官网来源结果保存前缀（GCS 路径前缀） | `report_sources` |
| `REPORT_SOURCE_CACHE_TTL_SECONDS` | 财报官网来源缓存 TTL（秒） | `86400` |
| `REPORT_SOURCE_MAX_CANDIDATES` | 单 ticker 最大候选 URL 数 | `24` |
| `REPORT_SOURCE_ENABLE_AI` | 是否启用 Vertex AI 复核（`1/true` 启用） | `0` |
| `VERTEX_PROJECT` | Vertex AI 项目 ID（未设时回退 `GOOGLE_CLOUD_PROJECT`） | `""` |
| `VERTEX_LOCATION` | Vertex AI 区域 | `us-central1` |
| `REPORT_SOURCE_AI_MODEL` | Vertex AI 复核模型 | `gemini-1.5-flash-002` |
| `REPORT_SOURCE_GOOGLE_API_KEY` | （可选）Google Programmable Search API Key，用于补充候选 URL | `""` |
| `REPORT_SOURCE_GOOGLE_CX` | （可选）Google Programmable Search Engine CX | `""` |
| `REPORT_SOURCE_HTTPX_LOG_LEVEL` | `httpx` 日志级别（`INFO/WARNING/ERROR`）；默认 `INFO`（打印逐条请求） | `INFO` |

> **建议**：生产环境通过 Cloud Run 的 `--set-env-vars` 或 Secret Manager 配置。

---

## 本地开发

```bash
cd GCP/financial_engine
./run_local.sh
```

访问：`http://localhost:8080/docs` 查看 Swagger UI。

可选参数（环境变量）：
- `PORT=9000 ./run_local.sh`：改端口
- `RELOAD=0 ./run_local.sh`：关闭热更新
- `INSTALL_DEPS=0 ./run_local.sh`：跳过依赖安装

---

## Docker 构建与运行

```bash
docker build -t financial-engine:latest .

docker run -p 8080:8080 \
  -e GCS_BUCKET_NAME=financial-data-engine-bucket \
  -e ENGINE_TZ=America/Los_Angeles \
  -e TRADING_DATA_ENGINE_URL=https://trading-data-engine-xxxxx-uc.a.run.app \
  -e FINANCIAL_L1_HIT_TTL_SECONDS=600 \
  -e FINANCIAL_L1_MISS_TTL_SECONDS=120 \
  -e FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS=3 \
  financial-engine:latest
```

---

## 部署到 Cloud Run（示例）

```bash
./deploy_financial_engine.sh
```

脚本会：
- 使用 `--source .` 提交构建
- 设置 `GCS_BUCKET_NAME / ENGINE_TZ / TRADING_DATA_ENGINE_URL / L1 TTL / no-earnings fallback`
- 区域默认 `us-central1`（可在脚本开头修改）

---

## 核心特性与设计

### 1) 按需刷新 + 财报日驱动
- 先读缓存：L1（进程内）→ L2（GCS）；
- 是否刷新由 `next earnings day` 决定：
  - 若未到财报日：直接返回缓存；
  - 若已过财报日且缓存尚未在财报日后刷新：才调用 yfinance；
  - 刷新后写回 GCS，并更新 L1；
- yfinance 失败时，回退到 L2 stale 数据，保证可用性。

### 2) 增量合并（避免漏更）
- 上游刷新后会将季度/年度报表与历史 JSON **按 `date/index` 去重合并**：
  - 如果某季度数据被 Yahoo 后续修订，**新数据覆盖旧数据**；
  - 若无变化，不产生重复记录。

### 3) AI Context 生成与存储
- 每次刷新都会生成一份针对 LLM 的简要上下文（含若干要点），保存为：
  - `ai_context/{TICKER}/{YYYY-MM-DD}.txt`
- 同时维护**当日清单**：
  - `ai_context/daily_index/{YYYY-MM-DD}.json`
  - 内容为当日所有已写入 Context 的 `{ticker, path}` 列表，图卡可一次拉取。

### 4) 统一时区
- 通过 `ENGINE_TZ` 控制，默认 **America/Los_Angeles**。

---

## API 一览

> Base URL: `http://<host>:8080`

### 健康检查
- `GET /health` → `{ "status": "ok" }`

### 获取并保存财报（单支）
- `POST /refresh/{ticker}`
  - 拉取 + 合并 JSON + 生成/保存当日 AI context + 更新当日清单
- `POST /save/{ticker}`
  - 仅拉取 + 合并 JSON
- `GET /financial/{ticker}`
  - 返回完整财报对象（支持 `force_refresh=0/1`，默认走 L1/L2 + 财报日刷新策略）
- `GET /earnings/{ticker}`
  - 返回财报解读 + 结构化财报因子信号（支持 `force_refresh=0/1`，默认走 L1/L2 + 财报日刷新策略）
- `GET /stockflow/fundamental/{ticker}`
  - 返回供 StockFlow 融合使用的财报因子分数、因子贡献和总信号

### 批量刷新
- `POST /batch_process_all`  
  刷新默认股票列表（`main.py` 中 `default_tickers`）
- `POST /batch_refresh`  
  请求体：`{"tickers": ["AAPL","MSFT",...]}`

### AI Context（供 QA / 卡片使用）
- `GET /ai_context/daily_index?date=YYYY-MM-DD`  
  返回当日清单：`[{"ticker":"AAPL","path":"gs://.../ai_context/AAPL/2025-08-11.txt"}, ...]`
- `GET /ai_context/{ticker}/by_date/{date}`  
  返回单个路径：`{"ticker":"AAPL","date":"2025-08-11","path":"gs://.../ai_context/AAPL/2025-08-11.txt"}`

### 财报官网来源发现与验证（Report Source）
- `GET /stockflow/report_source/{ticker}?force_refresh=0|1`  
  获取单只股票财报官网来源（IR/Reports/SEC）并返回验证结果。
- `POST /stockflow/report_source/batch_refresh`  
  请求体：`{"tickers":["AAPL","MSFT"],"force_refresh":true}`，批量发现与验证。
- `GET /stockflow/report_source/catalog/list?limit=500&ticker_prefix=A`  
  获取已缓存目录（优先 GCS，失败回退本地 `data/`）。
- `GET /report_source/catalog`  
  内置可视化目录页，可直接浏览、批量刷新、单条刷新（用于人工快速验收）。

### 本地结果回灌到 GCS（给 StockFlow 直接使用）
- 本地缓存文件位置：`GCP/financial_engine/data/*_report_source.json`
- GCS 目标路径格式：`gs://<bucket>/<REPORT_SOURCE_PREFIX>/<TICKER>.json`（默认 `report_sources`）

可先预览再上传：
```bash
cd GCP/financial_engine
python scripts/sync_report_source_local_to_gcs.py --bucket <your-bucket> --dry-run
python scripts/sync_report_source_local_to_gcs.py --bucket <your-bucket>
```

---

## 快速验证（本地 + GCP 通用）

### 本地
1. 启动服务：
   ```bash
   cd GCP/financial_engine
   ./run_local.sh
   ```
2. 打开目录页：`http://localhost:8080/report_source/catalog`
3. 点击 `Resolve Input Tickers`，输入如 `AAPL,MSFT,NVDA`，观察：
   - `verification_status`（`verified/partial/not_found`）
   - `IR / Reports / SEC` 链接可否打开
4. 也可直接调用 API：
   ```bash
   curl "http://localhost:8080/stockflow/report_source/AAPL?force_refresh=1"
   ```

### GCP（Cloud Run）
1. 部署后访问：`https://<financial-engine-service-url>/report_source/catalog`
2. 同步可用 API：
   - `https://<service-url>/stockflow/report_source/{ticker}`
   - `https://<service-url>/stockflow/report_source/catalog/list`
3. 建议只对内网或管理员开放该页面/接口（例如 Cloud Run IAM、IAP 或反向代理鉴权）。

---

## 与 QA / 卡片服务的协作建议

- **卡片（批量）**：每天调用 `GET /ai_context/daily_index?date=YYYY-MM-DD` 得到 path 列表，批量拼装 LLM 输入。
- **问答（单支）**：调用 `GET /ai_context/{ticker}/by_date/{date}` 获取指定股票当日（或历史某日）的 Context 路径。
- 两者都**不需要**自己拼 GCS 路径，解耦数据职责。

---

## 日志与错误处理
- 使用 Python `logging` 标准库输出日志。
- 批量任务对单支失败会记录错误并继续，不会中断整个批次。

---

## 版本
- `v1.1.0`
  - 增量合并 + AI Context 每日落盘 + 当日清单 + 批量接口 + 时区 ENV
