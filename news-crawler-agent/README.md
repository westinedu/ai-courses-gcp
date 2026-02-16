# News Crawler Agent

一个用于抓取股票相关新闻的独立服务。本项目从公开的 RSS 源获取新闻，
尝试提取正文或摘要，并将原始数据存储到本地文件系统或 Google Cloud
Storage。它提供了一系列接口，用于批量抓取、增量抓取和查询存储
数据目录。

## 功能特点

- **批量抓取**：针对指定日期和股票列表，批量获取当天的新闻。
- **增量抓取**：针对单支股票，即使当天已经抓取过，也能获取新的新闻
  并避免重复。
- **存储管理**：支持本地存储和 GCS 存储两种后端，灵活切换。
- **去重机制**：通过维护每日抓取清单，避免重复抓取同一新闻。
- **正文抽取**：使用 [`trafilatura`](https://github.com/adbar/trafilatura)
  自动提取网页正文，若提取失败，则回退到页面摘要。

## 目录结构

抓取到的原始新闻将按照日期和股票代码分层存储，结构如下：

```
raw-news/
  └─ 2025-08-13/
      ├─ AAPL/
      │   ├─ 083012_Verge_apple-earnings_12345678.json
      │   └─ ...
      ├─ GOOGL/
      │   └─ 093001_Axios_perplexity-bids-chrome_abcd1234.json
      └─ .manifest.json
```

每个 JSON 文件记录一条新闻的原始数据，包括标题、链接、发布日期、
新闻来源、摘要以及正文等字段。`.manifest.json` 用于记录当天已保存
新闻的 URL 哈希列表，确保增量抓取时不会重复存储。

## 配置

配置项通过环境变量或 `.env` 文件提供，具体参数说明请参见
[`settings.py`](./settings.py)。关键环境变量包括：

| 变量名 | 默认值 | 作用 |
|-------|-------|------|
| `STORAGE_BACKEND` | `local` | 存储后端：`local` 或 `gcs` |
| `LOCAL_STORAGE_ROOT` | `/data` | 本地存储根目录（local 模式） |
| `GCS_BUCKET_NAME` | — | GCS 桶名（gcs 模式） |
| `GCS_BASE_PREFIX` | `raw-news` | GCS 存储前缀 |
| `GCS_TOPIC_NEWS_PREFIX` | `topic-news` | 主题新闻在存储中的前缀 |
| `GCS_PERSON_NEWS_PREFIX` | `person-news` | 人物新闻在存储中的前缀 |
| `DEFAULT_TICKERS` | `AAPL,MSFT,GOOGL,AMZN,META` | 批量抓取时默认的股票列表 |
| `MAX_ARTICLES_PER_TICKER` | `30` | 每支股票抓取的最大新闻数 |
| `ENABLE_YAHOO_FINANCE` | `1` | 是否启用 Yahoo Finance RSS |
| `ENABLE_GOOGLE_NEWS` | `1` | 是否启用 Google News RSS |
| `TOPIC_CONFIG_LOCAL_PATH` | `./topic_configs.json` | 本地 topic 配置文件 |
| `TOPIC_CONFIG_GCS_BLOB` | — | GCS 上的 topic 配置 blob（可选） |
| `PERSONS_CONFIG_LOCAL_PATH` | `./persons_config.json` | 本地人物配置文件 |
| `PERSONS_CONFIG_GCS_BLOB` | — | GCS 上的人物配置 blob（可选） |

## 快速开始

### 本地运行

1. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 设置存储目录（可选）：

   ```bash
   export STORAGE_BACKEND=local
   export LOCAL_STORAGE_ROOT=./data
   ```

3. 启动服务：

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8080
   ```

4. 调用接口示例：

   - 批量抓取今日新闻：

     ```bash
     curl -X POST http://localhost:8080/crawl/daily \
       -H "Content-Type: application/json" \
       -d '{"tickers": ["GOOGL"], "date": "2025-08-13"}'
     ```

   - 增量抓取某支股票：

     ```bash
     curl -X POST http://localhost:8080/crawl/ticker/GOOGL/incremental \
       -H "Content-Type: application/json" \
       -d '{"date": "2025-08-13"}'
     ```

   - 查询某日或某股票的目录：

     ```bash
     curl "http://localhost:8080/gcs/paths?date=2025-08-13&ticker=GOOGL"
     ```

### 部署到 Cloud Run

1. 构建容器镜像：

   ```bash
   gcloud builds submit --tag gcr.io/PROJECT_ID/news-crawler-agent:0.1.0
   ```

2. 部署到 Cloud Run：

   ```bash
   gcloud run deploy news-crawler-agent \
     --image gcr.io/PROJECT_ID/news-crawler-agent:0.1.0 \
     --platform managed \
     --region us-central1 \
     --set-env-vars STORAGE_BACKEND=gcs,GCS_BUCKET_NAME=YOUR_BUCKET,GCS_BASE_PREFIX=raw-news,DEFAULT_TICKERS=AAPL,GOOGL,MSFT \
     --allow-unauthenticated --port 8080
   ```

## 接口说明

### 股票相关接口

#### `POST /crawl/daily`

批量抓取指定日期的新闻。请求体支持以下字段：

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `tickers` | `List[str]` | 否 | 股票代码列表，省略则使用 `DEFAULT_TICKERS` |
| `date` | `str` | 否 | 抓取日期，格式 `YYYY-MM-DD`，默认当天 |
| `force` | `bool` | 否 | 是否忽略去重逻辑，默认 `false` |
| `max_articles` | `int` | 否 | 每支股票抓取的最大新闻数，默认 `MAX_ARTICLES_PER_TICKER` |

### 原始数据目录查询

#### `GET /gcs/paths`

查询存储目录。查询参数：

| 参数 | 必填 | 描述 |
|------|------|------|
| `date` | 否 | 查询日期，默认为当天 |
| `ticker` | 否 | 股票代码，如省略则返回该日期的全部目录 |

返回示例：

```json
{
  "prefix": "raw-news/2025-08-13/GOOGL/",
  "objects": [
    "093001_Axios_perplexity-bids-chrome_abcd1234.json",
    "103501_Ver\u2639ge_other-news_efgh5678.json"
  ]
}
```

### `POST /crawl/ticker/{ticker}/incremental`

针对单支股票抓取增量新闻。路径参数 `ticker` 指定股票代码。
请求体支持以下字段：

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `date` | `str` | 否 | 抓取日期，格式 `YYYY-MM-DD`，默认当天 |
| `force` | `bool` | 否 | 是否忽略去重逻辑 |
| `max_articles` | `int` | 否 | 抓取条数上限，默认 `MAX_ARTICLES_PER_TICKER` |

返回结果类似于批量抓取，包含抓取成功的条目数量和忽略的 URL 数量等信息。

### 主题新闻接口

#### `POST /crawl/topic/macro/fed-funds-rate`

仅抓取联邦基金利率相关原始新闻，写入 `topic-news/{date}/macro/Fed_Funds_Rate/`，默认不会生成 AI Context。常规调度通常直接使用下方批处理接口。

#### `POST /batch/process_topic/macro/fed-funds-rate`

推荐的宏观批处理接口：先抓取 Fed Funds Rate 新闻，再运行 AI Context 管线并更新 `ai-context/daily_index`，供 QA/卡片服务消费。

#### `POST /crawl/topic/dynamic/{topic_key}`

按 `topic_configs.json` 的动态配置抓取任意主题，支持使用 key、`topic_identifier` 或别名。

### 人物新闻接口

#### `POST /crawl/person/{person_key}`

抓取 `persons_config.json` 中定义的名人/专家新闻或社交动态，写入 `person-news/{date}/{person_path}/`。

- 请求体字段与 `TopicRequest` 相同，可指定日期、强制抓取、最大数量。
- 配置的 `rss_sources`、关键字、白名单等与 topic 一致，未来可扩展到 X.com、NER 过滤等。

#### `POST /batch/process_person/dynamic/{person_key}`

按人物配置执行“抓取 + AI Context”批处理，适合 Cloud Scheduler / Run Job。成功后会更新 `ai-context/daily_index`，并返回保存的步骤路径。

#### `POST /persons-config/refresh`

重新加载人物配置文件（本地或 GCS），便于在不重启服务的情况下更新人物列表。

### 诊断工具

#### `POST /diagnostics/rss`

对 `topic_configs.json` 中配置的 RSS 源执行抽样抓取与正文解析，输出每条 RSS 是否成功解析、正文是否抽取成功、被过滤的原因等调试信息。

- `topics` *(List[str], 可选)*：限定需要诊断的 topic key 或别名，省略则遍历所有配置。
- `max_entries_per_source` *(int, 可选，默认 3)*：每个 RSS 源最多抽查的条目数（通常 RSS 已按时间倒序返回）。
- `enable_debug_log` *(bool, 可选，默认 false)*：诊断期间是否临时把 `news_crawler_agent` logger 提升到 DEBUG，以便输出更细的调试日志。诊断结束后会恢复原日志级别。
- `refresh_configs` *(bool, 可选，默认 false)*：诊断前是否重新加载动态 topic 配置。

接口不会写入存储，也不会影响正常抓取流程，可安全用于排查覆盖率问题。

#### `POST /diagnostics/persons/rss`

与主题诊断类似，用于排查人物配置的 RSS 覆盖情况。请求体与 `POST /diagnostics/rss` 相同，内部会针对人物配置执行抽样抓取。

### Topic 抓取行为

- 所有 topic 仍遵循时间窗口检查（默认 48 小时，在 RSS 解析后立即执行）以及去重逻辑。
- 经过 `required_keywords` 等预过滤的 RSS 条目，默认都会保存原文；不再因正文长度或关键字缺失被过滤，只在诊断报告中记录潜在问题。
- 如果需要恢复旧的严格过滤行为，可在某个 topic 配置中显式设置 `"enforce_content_filters": true`，此时会继续依据 `require_full_text`、`min_content_length` 等规则过滤正文。

cat <<'TIP'

快速验证：
  1) 健康检查
     curl -s "$SERVICE_URL/health" | jq .

  2) 批量抓取（当天新闻）
     curl -s -X POST "$SERVICE_URL/crawl/daily" \
       -H 'Content-Type: application/json' \
       -d '{"tickers":["AAPL","GOOGL"],"max_articles":10}' | jq .

  3) 单票增量抓取
     curl -s -X POST "$SERVICE_URL/crawl/ticker/GOOGL/incremental" \
       -H 'Content-Type: application/json' \
       -d '{"max_articles":10}' | jq .

  4) 列出原料目录（按日期、可选ticker）
     DATE=$(date +%F)
     curl -s "$SERVICE_URL/gcs/paths?date=$DATE&ticker=GOOGL" | jq .

提示：
  - 若使用本地存储，请设置：STORAGE_BACKEND=local 以及 LOCAL_STORAGE_ROOT
  - 生产环境建议改为私有访问，并用 Cloud Scheduler + SA 调用
