# news_crawler/models.py
from datetime import date
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from settings import settings # *** 注意：这里导入 settings 的路径已修改 ***

class CrawlResult(BaseModel):
    ticker: str
    date: str
    new_count: int
    skipped_count: int
    total_count: int


class TopicCrawlResult(BaseModel):
    topic: str
    topic_path: str
    date: str
    new_count: int
    skipped_count: int
    total_count: int


class PersonCrawlResult(BaseModel):
    person: str
    person_path: str
    date: str
    new_count: int
    skipped_count: int
    total_count: int

# 用于 /crawl/daily 接口的请求体
class CrawlDailyRequest(BaseModel):
    tickers: Optional[List[str]] = Field(
        None,
        json_schema_extra={"example": settings.default_tickers.split(",")},
        description="股票代码列表，省略则使用配置中的 DEFAULT_TICKERS。"
    )
    date: Optional[str] = Field(
        None,
        json_schema_extra={"example": None},
        description="目标日期，格式 YYYY-MM-DD，省略则默认为今天。"
    )
    force: Optional[bool] = Field(
        False,
        json_schema_extra={"example": False},
        description="是否忽略去重逻辑，默认为 false。"
    )
    max_articles: Optional[int] = Field(
        None,
        json_schema_extra={"example": settings.max_articles_per_ticker},
        description="每支股票抓取的最大新闻数，默认 settings.max_articles_per_ticker。"
    )

# 用于 /crawl/ticker/{ticker}/incremental 接口的请求体
class TickerRequest(BaseModel):
    date: Optional[str] = Field(
        None,
        json_schema_extra={"example": None},
        description="抓取日期，格式 YYYY-MM-DD，默认当天。"
    )
    force: Optional[bool] = Field(
        False,
        json_schema_extra={"example": False},
        description="是否忽略去重逻辑，默认为 false。"
    )
    max_articles: Optional[int] = Field(
        None,
        json_schema_extra={"example": settings.max_articles_per_ticker},
        description="每支股票抓取的最大新闻数，默认 settings.max_articles_per_ticker。"
    )


class TopicRequest(BaseModel):
    date: Optional[str] = Field(
        None,
        json_schema_extra={"example": None},
        description="抓取日期，格式 YYYY-MM-DD，默认当天。"
    )
    force: Optional[bool] = Field(
        False,
        json_schema_extra={"example": False},
        description="是否忽略去重逻辑，默认为 false。"
    )
    max_articles: Optional[int] = Field(
        None,
        json_schema_extra={"example": 30},
        description="抓取的最大新闻数，省略则使用动态配置中的默认值。"
    )


class PersonRequest(TopicRequest):
    """人物抓取请求体，字段与 TopicRequest 相同，单独定义便于扩展。"""

# 用于 /generate-ai-context/daily 接口的请求体
class GenerateDailyAIContextRequest(BaseModel):
    date: Optional[str] = Field(
        None,
        json_schema_extra={"example": None},
        description="目标日期，格式 YYYY-MM-DD，省略则默认为今天。"
    )

# 用于 /generate-ai-context/ticker/{ticker} 接口的请求体
class GenerateTickerAIContextRequest(BaseModel):
    date: Optional[str] = Field(
        None,
        json_schema_extra={"example": None},
        description="目标日期，格式 YYYY-MM-DD，省略则默认为今天。"
    )


class RSSDiagnosticsRequest(BaseModel):
    topics: Optional[List[str]] = Field(
        None,
        description="限定诊断的主题键列表，省略则检查所有主题。"
    )
    max_entries_per_source: Optional[int] = Field(
        3,
        ge=1,
        le=10,
        description="每个 RSS 源用于诊断的最大文章数。"
    )
    enable_debug_log: Optional[bool] = Field(
        False,
        description="是否在诊断期间临时开启 DEBUG 日志。"
    )
    refresh_configs: Optional[bool] = Field(
        False,
        description="诊断前是否先刷新动态配置。"
    )


class RSSSourceEntryDiagnostic(BaseModel):
    title: str
    published: Optional[str]
    raw_url: str
    canonical_url: str
    full_text_ok: bool
    full_text_length: int
    summary_length: int
    passes_pre_filters: bool
    passes_content_filters: bool
    rejection_reasons: List[str] = Field(default_factory=list)


class RSSSourceDiagnostic(BaseModel):
    topic_key: str
    topic_identifier: Optional[str]
    rss_url: str
    fetch_ok: bool
    entry_count: int
    errors: List[str] = Field(default_factory=list)
    entries: List[RSSSourceEntryDiagnostic] = Field(default_factory=list)


class RSSDiagnosticsResponse(BaseModel):
    total_sources: int
    diagnostics: List[RSSSourceDiagnostic]
