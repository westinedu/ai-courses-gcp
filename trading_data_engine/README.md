# 📈 交易数据服务 (Trading Data Service)

`trading-data-service` 是一个基于 **FastAPI** 构建的交易数据微服务，负责：

- 批量抓取与更新 **yfinance** 的股票历史 K 线数据。
- **无条件增量更新**：每次批量都会获取“最新日”数据并与历史 JSON 合并（按 `日期` 去重，新数据覆盖旧数据）。
- 生成**面向 AI 的上下文**（AI context），包括：技术指标、最近一个月每日 K 线、最近一年每月 K 线。生成的 AI Context 数据每天为每只股票落一份 JSON，并维护**当日清单**供图卡/QA 调用。
- 将原始历史数据 JSON 与 AI context JSON 写入 **Google Cloud Storage (GCS)**。
- 提供健康检查与批量调度接口，适合部署到 **Google Cloud Run**。

---

## 目录结构

```
trading_data_engine/
├── Dockerfile
├── requirements.txt
├── main.py                           # FastAPI 主程序
├── historical_data_local_fallback/   # 本地开发用：历史数据 fallback 目录
└── ai_context_local_fallback/        # 本地开发用：AI Context fallback 目录
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
| `GCS_BUCKET_NAME` | 用于保存历史数据 JSON 与 AI context JSON 的 GCS 桶 | `trading-data-engine-bucket` |
| `ENGINE_TZ` | 时区（用于 Context 时间戳、调度等） | `America/Los_Angeles` |

> **建议**：生产环境通过 Cloud Run 的 `--set-env-vars` 或 Secret Manager 配置。

---

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

访问：`http://localhost:8080/docs` 查看 Swagger UI。

**注意**：在本地运行时，如果未设置 `GCS_BUCKET_NAME` 环境变量，历史数据和 AI Context 将默认存储在项目根目录下的 `historical_data_local_fallback/` 和 `ai_context_local_fallback/` 目录中。这些数据在容器或 Cloud Run 实例重启后会丢失，仅用于本地开发测试。

---

## Docker 构建与运行

```bash
docker build -t trading-data-service:latest .

docker run -p 8080:8080 \
  -e GCS_BUCKET_NAME=your-trading-data-bucket \
  -e ENGINE_TZ=America/Los_Angeles \
  trading-data-service:latest
```

---

## 部署到 Cloud Run（示例）

1.  **构建 Docker 镜像并推送到 Artifact Registry**:
    ```bash
    gcloud auth configure-docker
    gcloud builds submit --tag gcr.io/your-gcp-project-id/trading-data-service .
    ```
2.  **部署到 Cloud Run**:
```bash
./deploy_trading_data_engine.sh
```
    *   `your-gcp-project-id`：您的 GCP 项目 ID。
    *   `your-gcp-region`：您希望部署的区域（例如 `us-central1`）。
    *   `your-gcs-bucket-name`：您在 GCP 中创建的存储桶名称。

### Google Cloud Scheduler (推荐)

为了实现数据的每日增量更新和 AI Context 的批量生成，推荐使用 Google Cloud Scheduler 定时触发对应的 API 端点。

*   **每日历史数据更新**:
    *   **目标 URL**: `YOUR_CLOUD_RUN_SERVICE_URL/trading_data/daily_update_all_historical`
    *   **频率**: 每天一次（例如，美东时间收盘后，如 `0 18 * * *` 表示 UTC 18点，即太平洋时间上午11点）。
    *   **方法**: `POST`
*   **AI Context 批量生成**:
    *   **目标 URL**: `YOUR_CLOUD_RUN_SERVICE_URL/trading_data/generate_ai_context_batch`
    *   **频率**: 每天一次（在历史数据更新完成后，例如 `30 18 * * *`）。
    *   **方法**: `POST`

确保 Cloud Scheduler 服务账号具有调用 Cloud Run 服务的权限。

---

## 核心特性与设计

### 1) 无条件增量更新（避免漏更）
- 服务不会试图“查询最新交易日”。
- 每次批量都会从 `yfinance` 拉取**最新几日**的数据（period='5d', interval='1d'），并将日 K 数据**按 `Date` 索引合并**：
  - 如果某日数据被 Yahoo 后续修订，**新数据会覆盖旧数据**。
  - 若无变动，则不会产生重复（去重合并）。
  - 该操作确保历史数据文件始终最新。

### 2) AI Context 生成与存储
- AI Context 在 **`generate_ai_context_batch`** 调用时生成，包含以下信息：
  - **最新技术特征**: 从历史数据中计算出的 RSI、MACD、均线等指标。
  - **最近一个月每日交易数据**: 提供股票最近一个月的详细日 K 线走势。
  - **最近一年每月 K 线数据**: 提供股票最近一年的宏观月 K 线概览。
- 生成的 AI Context 数据以 JSON 格式保存为：
  - `ai_context/{TICKER}_ai_context.json`
- 每个股票的 AI Context 文件都是**一个独立的文件**，包含了其所有 AI 所需的交易信息，方便一次性获取。

### 3) 统一时区
- 通过 `ENGINE_TZ` 环境变量控制，默认 **America/Los_Angeles**。用于数据的时间戳和调度等。

---

## API 一览

> Base URL: `http://<host>:8080`

### 健康检查
- `GET /health`
    *   **主要行为**: 提供一个简单的健康检查接口。
    *   **关键行动**:
        *   **获取数据**: ❌ 不会。
        *   **生成 AI Context**: ❌ 不会。
        *   **保存 JSON**: ❌ 不会。
        *   **提供路径**: ❌ 不会。
    *   **用途**: 用于 Cloud Run 或其他容器编排平台进行服务健康检查。

### 实时行情与历史数据查询

### 1. `GET /trading_data/{ticker}` - 获取实时行情数据
-   **主要行为**: 获取指定股票的实时行情数据。
-   **关键行动**:
    *   **获取数据**: ✅ 会。实时从 `yfinance` 获取指定股票的最新行情信息。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ❌ 不会。
    *   **提供路径**: ❌ 不会。（直接返回数据内容）
-   **用途**: 适用于需要获取指定股票最新、最原始的实时行情数据的场景，不涉及数据持久化或 AI Context 生成。

### 2. `GET /trading_data/{ticker}/historical` - 获取历史行情数据
-   **主要行为**: 获取指定股票的历史 K 线数据。
-   **关键行动**:
    *   **获取数据**: ✅ 会。实时从 `yfinance` 获取指定周期（'3y', '1y', '3mo', '5y'）的历史日 K 线数据。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ❌ 不会。
    *   **提供路径**: ❌ 不会。（直接返回数据内容）
-   **用途**: 适用于需要获取指定股票最新、最原始的历史 K 线数据的场景，不涉及数据持久化或 AI Context 生成。

### 3. `GET /trading_data/{ticker}/features` - 获取行情特征数据
-   **主要行为**: 获取指定股票的交易特征数据（技术指标、信号等）。
-   **关键行动**:
    *   **获取数据**: ✅ 会。实时从 `yfinance` 获取原始数据，并计算包括 RSI、MACD、移动平均线、趋势和买卖信号等特征。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ❌ 不会。
    *   **提供路径**: ❌ 不会。（直接返回数据内容）
-   **用途**: 适用于需要获取指定股票最新、最原始的交易特征数据的场景，不涉及数据持久化或 AI Context 生成。

### 历史数据管理与AI Context生成接口

### 4. `POST /trading_data/{ticker}/backfill_5y` - 手动触发获取并保存5年铺底交易数据到GCS
-   **主要行为**: 手动触发获取指定股票的5年历史 K 线数据并保存到 GCS。
-   **关键行动**:
    *   **获取数据**: ✅ 会。从 `yfinance` 获取5年历史数据。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ✅ 会。数据将持久化到 GCS 的 `historical_data/` 目录下。
    *   **提供路径**: ✅ 会。（返回保存后的 GCS 路径）
-   **用途**: 适用于首次部署或数据丢失后，对单个股票进行历史数据铺底的场景。此操作耗时较长，建议谨慎手动触发。

### 5. `POST /trading_data/daily_update_all_historical` - 每日增量历史数据更新 (Cloud Scheduler 调用)
-   **主要行为**: 为所有默认关注的股票执行每日增量历史数据更新。
-   **关键行动**:
    *   **获取数据**: ✅ 会。加载 GCS 中已有的历史数据，并从 `yfinance` 获取最新日 K 线数据，进行合并和去重。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ✅ 会。更新后的历史数据将持久化到 GCS 的 `historical_data/` 目录下。
    *   **提供路径**: ❌ 不会。（返回处理状态，不返回文件路径）
-   **用途**: 由 Google Cloud Scheduler 等外部调度器每日定时调用，确保历史数据是最新的。

### 6. `POST /trading_data/generate_ai_context_batch` - 批量生成AI分析上下文数据 (Card Job 调用)
-   **主要行为**: 批量生成所有默认关注股票的 AI 分析上下文数据。
-   **关键行动**:
    *   **获取数据**: ✅ 会。从 GCS 加载已保存的历史数据，并在此基础上计算技术特征、提取最近一个月的每日交易数据以及最近一年的每月 K 线数据。
    *   **生成 AI Context**: ✅ 会。
    *   **保存 JSON**: ✅ 会。生成的 AI Context 数据将持久化到 GCS 的 `ai_context/` 目录下。
    *   **提供路径**: ✅ 会。（返回每支股票生成结果及存储路径）
-   **用途**: 由 "Card Job" 或其他调度器定时调用，为 AI 模型或其他需要丰富分析上下文的应用提供预处理好的交易分析数据。

### AI Context 数据查询接口

### 7. `GET /trading_data/{ticker}/ai_context` - 获取指定股票的AI分析上下文数据
-   **主要行为**: 获取指定股票的预生成 AI 分析上下文数据。
-   **关键行动**:
    *   **获取数据**: ✅ 会。从 GCS 中加载已生成的 AI Context JSON 文件。
    *   **生成 AI Context**: ❌ 不会。
    *   **保存 JSON**: ❌ 不会。
    *   **提供路径**: ❌ 不会。（直接返回 AI Context 数据内容，而不是文件路径）
-   **用途**: 适用于 AI 模型或前端应用需要快速获取预处理好的交易数据分析上下文的场景。这些数据通常是定期通过批量任务预先生成的。

---

## 与 QA / 卡片服务的协作建议

-   **卡片（批量）**：
    *   每天调用 `POST /trading_data/generate_ai_context_batch` 触发所有股票 AI Context 的生成和存储。
    *   随后，卡片服务可以直接通过 `GET /trading_data/{ticker}/ai_context` 逐个获取已生成的 AI Context 数据，用于LLM输入或展示。
-   **问答（单支）**：
    *   用户提问时，直接调用 `GET /trading_data/{ticker}/ai_context` 获取指定股票的最新 AI Context 数据，作为 LLM 的补充信息。
-   **优势**: 统一存储在 GCS，服务解耦，数据职责清晰。

---

## 日志与错误处理
-   服务使用 Python `logging` 标准库输出日志，方便在 Cloud Logging 中查看。
-   批量任务对单支股票处理失败时，会记录错误并继续处理下一支股票，不会中断整个批次，保证了批量任务的鲁棒性。

---

## 版本
-   `v1.0.0`
    *   实时行情/历史 K 线/特征数据查询
    *   历史数据 GCS 增量更新与铺底
    *   AI Context 生成与 GCS 持久化
    *   批量任务接口
    *   统一时区配置
    *   健康检查

```

启动时如果 GCS 上已经存在 config/default_tickers.json，系统会优先加载 GCS 文件里的列表