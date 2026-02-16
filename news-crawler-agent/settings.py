"""
Settings Configuration for News Crawler Agent
=============================================

本模块定义了新闻抓取代理的所有可配置参数。这些配置项可以通过环境变量
来覆盖，也可以在本地开发时通过 `.env` 文件设置默认值。我们使用
``pydantic-settings`` 来实现类型安全的配置管理。

与原有的新闻引擎相同，优先级顺序如下：

1. 环境变量（部署到 Cloud Run 时通过 `--set-env-vars` 指定）
2. `.env` 文件（便于本地调试）
3. 本文件中定义的默认值

主要配置项：

- ``storage_backend``：支持 ``local`` 和 ``gcs``。在 ``local`` 模式下抓取
  到的新闻将存储在本地文件系统；在 ``gcs`` 模式下将写入 Google
  Cloud Storage。
- ``local_storage_root``：本地模式下存储原始新闻数据的根目录。
- ``gcs_bucket_name`` 与 ``gcs_base_prefix``：当使用 GCS 时指定
  桶名和前缀。
- ``default_tickers``：逗号分隔的股票代码列表，供批量抓取使用。
- ``enable_yahoo_finance`` 与 ``enable_google_news``：控制是否启用
  Yahoo Finance RSS 和 Google News RSS 源。
- ``timezone``：用于解析和生成日期的时区，例如 ``America/Los_Angeles``。

如需扩展新的环境变量或配置项，请参阅 ``pydantic`` 的文档。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os


class Settings(BaseSettings):
    """应用程序配置定义。

    本类继承自 ``BaseSettings``，会自动从环境变量中加载对应字段。
    如果某个字段在环境中不存在，则使用这里定义的默认值。若存在
    ``.env`` 文件，也会自动读取里面的键值对（仅在本地开发模式）。
    """

    # 应用名称，用于日志输出和识别
    app_name: str = os.getenv("APP_NAME", "News Crawler Agent")
    # 日志等级，支持 ``DEBUG``、``INFO``、``WARNING``、``ERROR`` 等
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # --- 存储配置 ---
    # 存储后端：``local`` 或 ``gcs``
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local")
    # 本地文件存储根目录，仅在 local 模式下有效
    local_storage_root: str = os.getenv("LOCAL_STORAGE_ROOT", "/data")
    # GCS 桶名称，仅在 gcs 模式下有效
    gcs_bucket_name: Optional[str] = os.getenv("GCS_BUCKET_NAME")
    # GCS 基础前缀，用于存储原始新闻数据（例如 ``raw-news``）
    gcs_base_prefix: str = os.getenv("GCS_BASE_PREFIX", "raw-news")
    # GCS 基础前缀，用于存储 AI 上下文文本文件（例如 ``ai-context``）
    gcs_ai_context_prefix: str = os.getenv("GCS_AI_CONTEXT_PREFIX", "ai-context")
    # GCS 前缀，用于存储主题类新闻（例如宏观或热点）
    gcs_topic_news_prefix: str = os.getenv("GCS_TOPIC_NEWS_PREFIX", "topic-news")
    # GCS 前缀，用于存储人物/名人新闻
    gcs_person_news_prefix: str = os.getenv("GCS_PERSON_NEWS_PREFIX", "person-news")

    # --- 新闻抓取配置 ---
    # 默认的股票代码列表，多个值用逗号分隔
    default_tickers: str = os.getenv(
        "DEFAULT_TICKERS", "AAPL,MSFT,GOOGL,AMZN,META,TSLA,NVDA,AMD,JPM,V,BRK-B,WMT,COST,KO,NKE,LLY,UNH,CAT,DIS,NFLX"
    )
    # 单支股票抓取的最大新闻条数
    max_articles_per_ticker: int = int(
        os.getenv("MAX_ARTICLES_PER_TICKER", "30")
    )
    # 是否启用 Yahoo Finance RSS 源
    enable_yahoo_finance: bool = os.getenv("ENABLE_YAHOO_FINANCE", "1") == "1"
    # 是否启用 Google News RSS 搜索（用于股票新闻）
    enable_google_news: bool = os.getenv("ENABLE_GOOGLE_NEWS", "1") == "1"
    # Google News RSS 模板
    google_news_rss_template: str = os.getenv(
        "GOOGLE_NEWS_RSS_TEMPLATE",
        "https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
    )

    # --- 主题新闻（宏观等）配置 ---
    enable_macro_fed_news: bool = os.getenv("ENABLE_MACRO_FED_NEWS", "1") == "1"
    macro_fed_topic_storage_path: str = os.getenv(
        "MACRO_FED_TOPIC_STORAGE_PATH",
        "macro/Fed_Funds_Rate",
    )
    macro_fed_topic_identifier: str = os.getenv(
        "MACRO_FED_TOPIC_IDENTIFIER",
        "macro.Fed_Funds_Rate",
    )
    macro_fed_rss_sources: str = os.getenv(
        "MACRO_FED_RSS_SOURCES",
        ",".join(
            [
                "https://www.federalreserve.gov/feeds/press_monetary.xml",
                "https://www.federalreserve.gov/feeds/press_all.xml",
                "https://www.marketwatch.com/rss/economy",
                "https://www.cnbc.com/id/10000664/device/rss/rss.html",
                "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
                "https://feeds.finance.yahoo.com/rss/2.0/headline?s=Federal%20Reserve&region=US&lang=en-US",
                "https://news.google.com/rss/search?q=%22Federal%20Reserve%22%20%22interest%20rate%22&hl=en-US&gl=US&ceid=US:en",
                "https://news.google.com/rss/search?q=%22Fed%20Funds%20Rate%22&hl=en-US&gl=US&ceid=US:en",
            ]
        ),
    )
    macro_fed_max_articles: int = int(
        os.getenv("MACRO_FED_MAX_ARTICLES", "30")
    )
    macro_fed_required_keywords: str = os.getenv(
        "MACRO_FED_REQUIRED_KEYWORDS",
        "Federal Reserve,Fed Funds Rate,Fed,interest rate,rate hike,rate cut,monetary policy,FOMC",
    )
    macro_fed_excluded_keywords: str = os.getenv(
        "MACRO_FED_EXCLUDED_KEYWORDS",
        "FedEx,Federal Express",
    )
    macro_fed_source_allowlist: str = os.getenv(
        "MACRO_FED_SOURCE_ALLOWLIST",
        "",
    )
    macro_fed_source_blocklist: str = os.getenv(
        "MACRO_FED_SOURCE_BLOCKLIST",
        "",
    )
    macro_fed_min_content_length: int = int(
        os.getenv("MACRO_FED_MIN_CONTENT_LENGTH", "400")
    )
    macro_fed_min_summary_length: int = int(
        os.getenv("MACRO_FED_MIN_SUMMARY_LENGTH", "80")
    )
    macro_fed_require_full_text: bool = os.getenv("MACRO_FED_REQUIRE_FULL_TEXT", "1") == "1"
    # 列表用于 Step 2 摘要高亮的优先关键词，逗号分隔，可按需覆盖或扩展。
    macro_fed_highlight_keywords: str = os.getenv(
        "MACRO_FED_HIGHLIGHT_KEYWORDS",
        "Federal Reserve,Fed,interest rate,rate cut,monetary policy,inflation,tariff,treasury,yield,economic,gdp,employment",
    )
    # --- Additional macro topic configurations ---
    enable_rate_vs_inflation_news: bool = os.getenv("ENABLE_RATE_VS_INFLATION_NEWS", "1") == "1"
    rate_vs_inflation_topic_storage_path: str = os.getenv(
        "RATE_VS_INFLATION_TOPIC_STORAGE_PATH", "macro/Rate_vs_Inflation"
    )
    rate_vs_inflation_topic_identifier: str = os.getenv(
        "RATE_VS_INFLATION_TOPIC_IDENTIFIER", "macro.Rate_vs_Inflation"
    )
    rate_vs_inflation_rss_sources: str = os.getenv(
        "RATE_VS_INFLATION_RSS_SOURCES",
        ",".join([
            # Yahoo-based searches and finance feeds tuned for CPI, inflation, prices and Fed analysis
            # Yahoo News (search) - generic inflation/CPI analysis
            "https://news.search.yahoo.com/rss?p=CPI+inflation+analysis",
            "https://news.search.yahoo.com/rss?p=consumer+price+index+CPI+analysis",
            "https://news.search.yahoo.com/rss?p=inflation+causes+analysis",
            "https://news.search.yahoo.com/rss?p=price+levels+inflation+analysis",
            # Yahoo Finance - tag/topic feeds and Fed related queries
            "https://finance.yahoo.com/news/rssindex",
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "https://news.search.yahoo.com/rss?p=Federal+Reserve+inflation",
            # Authoritative and analysis-focused outlets
            "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
            "https://www.theguardian.com/business/economics/rss",
            "https://feeds.bbci.co.uk/news/business/rss.xml",
        ])
    )
    rate_vs_inflation_max_articles: int = int(os.getenv("RATE_VS_INFLATION_MAX_ARTICLES", "30"))
    rate_vs_inflation_required_keywords: str = os.getenv("RATE_VS_INFLATION_REQUIRED_KEYWORDS", "rate,inflation,consumer price,cpi,core inflation")

    enable_rate_heatmap_news: bool = os.getenv("ENABLE_RATE_HEATMAP_NEWS", "1") == "1"
    rate_heatmap_topic_storage_path: str = os.getenv(
        "RATE_HEATMAP_TOPIC_STORAGE_PATH", "macro/Rate_Heatmap"
    )
    rate_heatmap_topic_identifier: str = os.getenv(
        "RATE_HEATMAP_TOPIC_IDENTIFIER", "macro.Rate_Heatmap"
    )
    rate_heatmap_rss_sources: str = os.getenv("RATE_HEATMAP_RSS_SOURCES", "")
    rate_heatmap_max_articles: int = int(os.getenv("RATE_HEATMAP_MAX_ARTICLES", "30"))
    rate_heatmap_required_keywords: str = os.getenv("RATE_HEATMAP_REQUIRED_KEYWORDS", "rate,interest rate,policy,central bank")

    enable_macro_dashboard_news: bool = os.getenv("ENABLE_MACRO_DASHBOARD_NEWS", "1") == "1"
    macro_dashboard_topic_storage_path: str = os.getenv(
        "MACRO_DASHBOARD_TOPIC_STORAGE_PATH", "macro/Macro_Dashboard"
    )
    macro_dashboard_topic_identifier: str = os.getenv(
        "MACRO_DASHBOARD_TOPIC_IDENTIFIER", "macro.Macro_Dashboard"
    )
    macro_dashboard_rss_sources: str = os.getenv("MACRO_DASHBOARD_RSS_SOURCES", "")
    macro_dashboard_max_articles: int = int(os.getenv("MACRO_DASHBOARD_MAX_ARTICLES", "40"))
    macro_dashboard_required_keywords: str = os.getenv("MACRO_DASHBOARD_REQUIRED_KEYWORDS", "economy,employment,gdp,inflation,policy,rate")

    # 默认时区，用于解释日期字符串和生成时间戳
    timezone: str = os.getenv("TIMEZONE", "Asia/Shanghai")

    # 新闻时间窗口（小时）。超过该时长的新闻会在抓取阶段被跳过（<=0 则不限制）
    news_max_age_hours: int = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))

    # Pydantic 配置：允许从 .env 文件读取，忽略额外字段
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # --- Dynamic topic config (optional) ---
    # 当使用 GCS 存储时，可以把 topic 配置 JSON 放到 GCS 桶中，格式为一个字典，
    # key 可以是 topic identifier、topic storage path 或自定义 key，value 为 topic 配置。
    topic_config_gcs_blob: Optional[str] = os.getenv("TOPIC_CONFIG_GCS_BLOB", "")
    # 本地调试时可指定本地 topic 配置文件路径（JSON）
    topic_config_local_path: str = os.getenv("TOPIC_CONFIG_LOCAL_PATH", "./topic_configs.json")
    # 是否在服务启动时自动从 GCS/local 加载 topic config
    topic_config_refresh_on_start: bool = os.getenv("TOPIC_CONFIG_REFRESH_ON_START", "1") == "1"

    # --- Persons (celebrity/expert) config ---
    persons_config_gcs_blob: Optional[str] = os.getenv("PERSONS_CONFIG_GCS_BLOB", "config/persons_config.json")
    persons_config_local_path: str = os.getenv("PERSONS_CONFIG_LOCAL_PATH", "./persons_config.json")
    persons_config_refresh_on_start: bool = os.getenv("PERSONS_CONFIG_REFRESH_ON_START", "1") == "1"


# 创建设置实例，加载配置
settings = Settings()

# 一些启动时的校验逻辑
if settings.storage_backend not in ("local", "gcs"):
    raise ValueError("STORAGE_BACKEND must be either 'local' or 'gcs'")
if settings.storage_backend == "gcs" and not settings.gcs_bucket_name:
    raise ValueError(
        "When STORAGE_BACKEND is 'gcs', GCS_BUCKET_NAME must be configured"
    )
