# news_crawler/core.py
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import feedparser  # type: ignore
import trafilatura  # type: ignore
from bs4 import BeautifulSoup
from slugify import slugify

from settings import settings # *** 注意：这里导入 settings 的路径已修改 ***
from news_crawler.utils import _parse_date, _get_date_dir # *** 从 utils 导入 _parse_date 和 _get_date_dir ***
from news_crawler.storage import (
    _load_gcs_manifest, _save_gcs_manifest, _save_gcs_article,
    _load_local_manifest, _save_local_manifest, _save_local_article,
    _list_gcs_objects, _list_local_objects # *** 导入 _list_gcs_objects 和 _list_local_objects ***
)
from news_crawler.models import (
    CrawlResult,
    TopicCrawlResult,
    PersonCrawlResult,
    RSSSourceDiagnostic,
    RSSSourceEntryDiagnostic,
) # 导入 CrawlResult
from news_crawler.ai_context import (
    _load_articles_for_ticker_date,
    _prepare_ai_context_for_articles,
    _save_ai_context_to_gcs,
    _prepare_ai_context_pipeline,
    _save_ai_context_step,
)
from news_crawler.dynamic_config import get_topic_config, get_all_topic_configs
from news_crawler.persons_config import get_person_config, get_all_person_configs


logger = logging.getLogger("news_crawler_agent")

# --- Helper Functions ---
def _keyword_in_text(text: str, keyword: str) -> bool:
    if not text or not keyword:
        return False
    # Use case-insensitive substring matching so variations like "treasurys" still satisfy
    # the configured keyword "treasury". The text blob already combines title/summary fields.
    text_norm = text.lower()
    keyword_norm = keyword.lower().strip()
    if not keyword_norm:
        return False
    return keyword_norm in text_norm


def _normalize_domain(domain: str) -> str:
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _extract_published_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    if "published_parsed" in entry and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC"))
        except Exception:
            return None
    published_str = entry.get("published")
    if not published_str:
        return None
    try:
        normalized = published_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(ZoneInfo("UTC"))
    except Exception:
        return None


def _is_entry_too_old(entry: Dict[str, Any], max_age_hours: int, reference: datetime) -> bool:
    if max_age_hours <= 0:
        return False
    published_dt = _extract_published_datetime(entry)
    if not published_dt:
        return False
    age_hours = (reference - published_dt).total_seconds() / 3600
    return age_hours > max_age_hours


def _compute_url_hash(url: str) -> str:
    """计算 URL 的短哈希值，用于去重和文件命名。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _resolve_entry_url(entry: Dict[str, Any]) -> Dict[str, str]:
    """Resolve the canonical article URL for an RSS entry.

    Google News RSS entries often point to a ``news.google.com`` redirect URL which
    prevents us from downloading the true article body.  This helper inspects the
    entry and tries several fallbacks (``feedburner_origlink``, query parameters,
    embedded links in the path) to recover the original article URL.  The raw RSS
    link is preserved so callers can keep it for diagnostics if needed.

    Returns a dictionary with ``canonical`` and ``raw`` URLs.  If resolution fails
    the canonical URL falls back to the raw link.
    """

    raw_url = (entry.get("link") or "").strip()
    canonical_url = raw_url

    if not raw_url:
        return {"canonical": "", "raw": ""}

    try:
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower()

        if "news.google." in host:
            # Preferred: explicit original link provided by the feed
            orig_link = (entry.get("feedburner_origlink") or "").strip()
            if orig_link:
                canonical_url = orig_link
            else:
                query = parse_qs(parsed.query)
                # Google sometimes stores the original URL in different parameters
                for key in ("url", "q", "u"):
                    values = query.get(key)
                    if values:
                        candidate = values[0].strip()
                        if candidate:
                            canonical_url = candidate
                            break

                if canonical_url == raw_url:
                    # Some Google RSS entries embed the article URL directly in the path
                    path = unquote(parsed.path)
                    http_index = path.find("http")
                    if http_index != -1:
                        candidate = path[http_index:]
                        candidate = candidate.replace("http:/", "http://", 1)
                        candidate = candidate.replace("https:/", "https://", 1)
                        if candidate:
                            canonical_url = candidate

            if canonical_url.startswith("//"):
                canonical_url = f"{parsed.scheme}:{canonical_url}"

        canonical_url = canonical_url.strip()
    except Exception as exc:
        logger.debug(
            "Failed to resolve canonical URL for entry. Falling back to raw link: %s | %s",
            raw_url,
            exc,
        )
        canonical_url = raw_url

    if canonical_url.startswith("http:/") and not canonical_url.startswith("http://"):
        canonical_url = canonical_url.replace("http:/", "http://", 1)
    if canonical_url.startswith("https:/") and not canonical_url.startswith("https://"):
        canonical_url = canonical_url.replace("https:/", "https://", 1)

    return {"canonical": canonical_url or raw_url, "raw": raw_url}


def _compose_entry_text(entry: Dict[str, Any]) -> str:
    title = entry.get("title") or ""
    summary = entry.get("summary") or entry.get("description") or ""
    source_title = (
        entry.get("source", {}).get("title")
        or entry.get("publisher")
        or entry.get("source_title")
        or ""
    )
    return " ".join([title, summary, source_title]).lower()


def _passes_topic_pre_filters(
    *,
    entry: Dict[str, Any],
    canonical_url: str,
    topic_config: Optional[Dict[str, Any]] = None,
) -> bool:
    config = topic_config or {}
    if not config:
        return True

    # Build a lowercased blob from title + summary + source for keyword checks.
    text_blob = _compose_entry_text(entry)
    required_keywords: List[str] = config.get("required_keywords", [])
    if required_keywords and not any(_keyword_in_text(text_blob, kw) for kw in required_keywords):
        return False

    excluded_keywords: List[str] = config.get("excluded_keywords", [])
    if excluded_keywords and any(_keyword_in_text(text_blob, kw) for kw in excluded_keywords):
        return False

    domain = _normalize_domain(urlparse(canonical_url).netloc) if canonical_url else ""
    if domain:
        allowlist: List[str] = config.get("source_allowlist", [])
        if allowlist and domain not in allowlist:
            return False
        blocklist: List[str] = config.get("source_blocklist", [])
        if blocklist and domain in blocklist:
            return False

    return True

def _passes_topic_filters(entry: Dict[str, Any], canonical_url: str, config: Optional[Dict[str, Any]]) -> bool:
    if not config:
        return True
    text_blob = _compose_entry_text(entry)
    if any(_keyword_in_text(text_blob, kw) for kw in config.get("excluded_keywords", [])):
        return False
    if config.get("required_keywords") and not any(
        _keyword_in_text(text_blob, kw) for kw in config.get("required_keywords", [])
    ):
        return False
    domain = _normalize_domain(urlparse(canonical_url).netloc) if canonical_url else ""
    if domain:
        allowlist: List[str] = config.get("source_allowlist", [])
        if allowlist and domain not in allowlist:
            return False
        blocklist: List[str] = config.get("source_blocklist", [])
        if blocklist and domain in blocklist:
            return False
    return True

def _passes_topic_content_filters(
    *,
    topic_config: Optional[Dict[str, Any]],
    full_text: Optional[str],
    feed_summary: Optional[str],
) -> bool:
    config = topic_config or {}
    if not config:
        return True

    cleaned_full_text = (full_text or "").strip()
    cleaned_summary = (BeautifulSoup(feed_summary, "html.parser").get_text(separator=" ") if feed_summary else "").strip()
    combined_text = "\n".join([cleaned_full_text, cleaned_summary]).lower()
    required_keywords: List[str] = config.get("required_keywords", [])

    require_full_text = config.get("require_full_text", False)
    if require_full_text and not cleaned_full_text:
        return False

    min_content_length = config.get("min_content_length", 0)
    if min_content_length and len(cleaned_full_text) < min_content_length:
        return False

    min_summary_length = config.get("min_summary_length", 0)
    if min_summary_length and not cleaned_full_text and len(cleaned_summary) < min_summary_length:
        return False

    if required_keywords and not any(_keyword_in_text(combined_text, kw) for kw in required_keywords):
        return False

    return True


def _diagnose_topic_pre_filters(
    *,
    entry: Dict[str, Any],
    canonical_url: str,
    topic_config: Optional[Dict[str, Any]],
) -> tuple[bool, List[str]]:
    """Return filter outcome plus reasons mirroring ``_passes_topic_pre_filters``."""

    config = topic_config or {}
    if not config:
        return True, []

    reasons: List[str] = []
    text_blob = _compose_entry_text(entry)

    required_keywords: List[str] = config.get("required_keywords", [])
    if required_keywords and not any(_keyword_in_text(text_blob, kw) for kw in required_keywords):
        reasons.append("missing_required_keyword")

    excluded_keywords: List[str] = config.get("excluded_keywords", [])
    if excluded_keywords and any(_keyword_in_text(text_blob, kw) for kw in excluded_keywords):
        reasons.append("hit_excluded_keyword")

    domain = _normalize_domain(urlparse(canonical_url).netloc) if canonical_url else ""
    if domain:
        allowlist: List[str] = config.get("source_allowlist", [])
        if allowlist and domain not in allowlist:
            reasons.append("source_not_allowlisted")
        blocklist: List[str] = config.get("source_blocklist", [])
        if blocklist and domain in blocklist:
            reasons.append("source_blocklisted")

    return not reasons, reasons


def _diagnose_topic_content_constraints(
    *,
    topic_config: Optional[Dict[str, Any]],
    full_text: Optional[str],
    feed_summary: Optional[str],
) -> tuple[bool, List[str], int, int]:
    """Return content-filter outcome, failure reasons and text lengths."""

    config = topic_config or {}
    cleaned_full_text = (full_text or "").strip()
    cleaned_summary = (
        BeautifulSoup(feed_summary, "html.parser").get_text(separator=" ")
        if feed_summary
        else ""
    ).strip()

    if not config:
        return True, [], len(cleaned_full_text), len(cleaned_summary)

    combined_text = "\n".join([cleaned_full_text, cleaned_summary]).lower()
    reasons: List[str] = []

    if config.get("require_full_text", False) and not cleaned_full_text:
        reasons.append("require_full_text")

    min_content_length = config.get("min_content_length", 0)
    if min_content_length and len(cleaned_full_text) < min_content_length:
        reasons.append(f"content_too_short<{min_content_length}")

    min_summary_length = config.get("min_summary_length", 0)
    if (
        min_summary_length
        and not cleaned_full_text
        and len(cleaned_summary) < min_summary_length
    ):
        reasons.append(f"summary_too_short<{min_summary_length}")

    required_keywords: List[str] = config.get("required_keywords", [])
    if required_keywords and not any(_keyword_in_text(combined_text, kw) for kw in required_keywords):
        reasons.append("missing_required_keyword")

    return not reasons, reasons, len(cleaned_full_text), len(cleaned_summary)


def _summarize_text(text: str, max_sentences: int = 3) -> str:
    """截取文本的前 ``max_sentences`` 个句子作为摘要。"""
    if not text:
        return ""
    sentences = re.split(r"(?<=[。.!?？!])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return " ".join(sentences[:max_sentences])


async def _extract_full_text(url: str) -> Optional[str]:
    """
    尝试使用 ``trafilatura`` 抓取并抽取网页正文。
    返回正文文本，如果失败则返回 ``None``。此函数会捕获所有异常并
    记录日志，不会抛出错误。
    """
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=True,
        )
        if extracted:
            cleaned = re.sub(r"\s+", " ", extracted).strip()
            return cleaned
    except Exception as e:
        logger.debug(f"正文抽取失败: {e} | URL: {url}")
    return None


def _build_article_json(
    ticker: str,
    date_str: str,
    entry: Dict[str, Any],
    full_text: Optional[str],
    feed_summary: Optional[str],
    url_hash: str,
    *,
    logical_ticker: Optional[str] = None,
    news_type: str = "stock",
    topic: Optional[str] = None,
    topic_group: Optional[str] = None,
    canonical_url: Optional[str] = None,
    raw_rss_link: Optional[str] = None,
) -> Dict[str, Any]:
    """根据 RSS 条目信息和抓取结果构建存储 JSON 对象。"""
    title: str = entry.get("title", "").strip()
    link: str = (canonical_url or entry.get("link", "") or "").strip()
    source: str = entry.get("source", {}).get("title") or entry.get("publisher") or entry.get("source_title") or ""
    published: Optional[str] = None
    if "published_parsed" in entry and entry.published_parsed:
        try:
            published_dt_utc = datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            published = published_dt_utc.isoformat()
        except Exception:
            logger.warning(f"Failed to parse published_parsed for entry: {entry.get('link')}. Using raw published string.")
            published = entry.get("published", "")
    else:
        published = entry.get("published", "")
    summary = ""
    if full_text:
        summary = _summarize_text(full_text, max_sentences=3)
    elif feed_summary:
        soup = BeautifulSoup(feed_summary, 'html.parser')
        cleaned_summary = soup.get_text(separator=' ').strip()
        summary = _summarize_text(cleaned_summary, max_sentences=3)
    logical_identifier = (logical_ticker or ticker).replace("/", "_")
    article_id = f"{date_str}-{logical_identifier}-{url_hash}"
    metrics: Dict[str, Any] = {
        "title_len": len(title),
        "content_len": len(full_text) if full_text else 0,
    }
    article: Dict[str, Any] = {
        "id": article_id,
        "ticker": logical_ticker or ticker,
        "date": date_str,
        "title": title,
        "url": link,
        "published": published,
        "source": source,
        "extraction": {
            "summary": summary,
            "content": full_text or "",
            "fulltext_ok": bool(full_text),
        },
        "metrics": metrics,
        "version": "0.1.0",
    }
    if raw_rss_link and raw_rss_link != link:
        article["rss_link"] = raw_rss_link
    if news_type:
        article["news_type"] = news_type
    if topic:
        article["topic"] = topic
    if topic_group:
        article["topic_group"] = topic_group
    return article


def _compute_dedupe_hash(entry: Dict[str, Any]) -> str:
    """
    计算一个更健壮的去重哈希，基于归一化标题、来源和精确到分钟的发布时间。
    """
    title = entry.get("title", "").strip()
    normalized_title = slugify(title).lower()

    source = entry.get("source", {}).get("title") or entry.get("publisher") or entry.get("source_title") or ""
    normalized_source = slugify(source).lower()

    published_minute = ""
    if "published_parsed" in entry and entry.published_parsed:
        try:
            published_minute = datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    dedupe_string = f"{normalized_title}|{normalized_source}|{published_minute}"
    return hashlib.sha256(dedupe_string.encode("utf-8")).hexdigest()

async def _crawl_and_save_entry(entry: Dict[str, Any], seen_hashes: set, date_str: str, storage_path: str, manifest: Dict, **kwargs):
    canonical_url = _resolve_entry_url(entry)
    if not canonical_url: return 0
    dedupe_hash = _compute_dedupe_hash(entry)
    if dedupe_hash in seen_hashes and not kwargs.get('force'): return 0
    
    if kwargs.get('config') and not _passes_topic_filters(entry, canonical_url, kwargs['config']): return 0

    full_text = await _extract_full_text(canonical_url)
    if kwargs.get('config', {}).get('require_full_text') and not full_text: return 0
    if kwargs.get('config', {}).get('min_content_length', 0) > 0 and len(full_text or "") < kwargs['config']['min_content_length']: return 0

    url_hash = _compute_url_hash(canonical_url)
    article_json = _build_article_json(entry, full_text, url_hash, date_str=date_str, canonical_url=canonical_url, **kwargs)
    
    save_func = _save_gcs_article if settings.storage_backend == "gcs" else _save_local_article
    rel_path = await save_func(article_json, date_str, storage_path, url_hash, base_prefix=kwargs.get("base_prefix"))
    
    seen_hashes.add(dedupe_hash)
    manifest.setdefault("hashes", []).append(dedupe_hash)
    manifest.setdefault("files", []).append(rel_path)
    return 1

async def _crawl_source(feed_urls: List[str], max_articles: int, max_age_hours: int) -> List[Dict[str, Any]]:
    entries = []
    for url in feed_urls:
        try:
            entries.extend(feedparser.parse(url).entries)
        except Exception as e:
            logger.warning(f"Failed to parse RSS feed: {url} | {e}")
    now_utc = datetime.now(ZoneInfo("UTC"))
    filtered_entries = [e for e in entries if not _is_entry_too_old(e, max_age_hours, now_utc)]
    return sorted(filtered_entries, key=_extract_published_datetime, reverse=True)[:max_articles]


async def _crawl_ticker(
    ticker: str,
    date_obj: date,
    max_articles: int,
    force: bool,
) -> CrawlResult:
    """抓取单支股票在指定日期的新闻。"""
    date_str = _get_date_dir(date_obj)
    new_count = 0
    skipped_count = 0

    if settings.storage_backend == "gcs":
        manifest = _load_gcs_manifest(date_str)
    else:
        manifest = _load_local_manifest(date_str)
    seen_dedupe_hashes: set[str] = set(manifest.get("hashes", []))

    feed_urls: List[str] = []
    if settings.enable_yahoo_finance:
        feed_urls.append(
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        )
    if settings.enable_google_news:
        feed_urls.append(
            settings.google_news_rss_template.format(ticker=ticker)
        )
    
    entries: List[Dict[str, Any]] = []
    for url in feed_urls:
        try:
            # feedparser parses each RSS/Atom feed into an object whose .entries is already ordered
            # (usually newest first). We collect all entries first, then sort again just in case.
            parsed = feedparser.parse(url)
            entries.extend(parsed.entries)
        except Exception as e:
            logger.warning(f"解析 RSS 失败: {url} | {e}")
    total_count = len(entries)

    def sort_key(e: Dict[str, Any]):
        if "published_parsed" in e and e.published_parsed:
            try:
                return datetime(*e.published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                logger.warning(f"Failed to parse published_parsed for sorting: {e}")
        return datetime.min.replace(tzinfo=ZoneInfo("UTC"))
    entries.sort(key=sort_key, reverse=True)
    
    entries = entries[:max_articles]

    now_utc = datetime.now(ZoneInfo("UTC"))

    for entry in entries:
        url_info = _resolve_entry_url(entry)
        canonical_url = url_info.get("canonical", "")
        raw_url = url_info.get("raw", "")

        current_url = canonical_url or raw_url
        if not current_url:
            skipped_count += 1
            continue

        if _is_entry_too_old(entry, settings.news_max_age_hours, now_utc):
            skipped_count += 1
            continue

        current_dedupe_hash = _compute_dedupe_hash(entry)

        if current_dedupe_hash in seen_dedupe_hashes and not force:
            skipped_count += 1
            continue
        if raw_url and canonical_url and raw_url != canonical_url:
            logger.debug("Resolved canonical URL for %s: %s -> %s", ticker, raw_url, canonical_url)

        full_text = await _extract_full_text(current_url)
        feed_summary = entry.get("summary", "") or entry.get("description", "")
        
        article_json = _build_article_json(
            ticker=ticker,
            date_str=date_str,
            entry=entry,
            full_text=full_text,
            feed_summary=feed_summary,
            url_hash=_compute_url_hash(current_url),
            canonical_url=current_url,
            raw_rss_link=raw_url if raw_url and raw_url != current_url else None,
        )
        try:
            url_hash = _compute_url_hash(current_url)
            if settings.storage_backend == "gcs":
                rel_path = await _save_gcs_article(article_json, date_str, ticker, url_hash)
            else:
                rel_path = _save_local_article(article_json, date_str, ticker, url_hash)
            
            seen_dedupe_hashes.add(current_dedupe_hash)
            manifest.setdefault("hashes", []).append(current_dedupe_hash)
            manifest.setdefault("files", []).append(rel_path)
            new_count += 1
        except Exception as e:
            logger.error(f"保存文章失败: {ticker} | {current_url} | {e}")
            skipped_count += 1
    
    if settings.storage_backend == "gcs":
        _save_gcs_manifest(date_str, manifest)
    else:
        _save_local_manifest(date_str, manifest)
    return CrawlResult(
        ticker=ticker,
        date=date_str,
        new_count=new_count,
        skipped_count=skipped_count,
        total_count=total_count,
    )


async def _crawl_entity(
    *,
    entity_identifier: str,
    entity_storage_path: str,
    entity_group: str,
    news_type: str,
    date_obj: date,
    feed_urls: List[str],
    max_articles: int,
    force: bool,
    max_age_hours: Optional[int],
    entity_config: Optional[Dict[str, Any]],
    storage_prefix: Optional[str],
) -> Dict[str, Any]:
    """Shared crawler logic for topic/person style entities."""

    date_str = _get_date_dir(date_obj)
    new_count = 0
    skipped_count = 0

    effective_prefix = storage_prefix or settings.gcs_topic_news_prefix

    if settings.storage_backend == "gcs":
        manifest = _load_gcs_manifest(date_str, base_prefix=effective_prefix)
    else:
        manifest = _load_local_manifest(date_str, base_prefix=effective_prefix)

    seen_dedupe_hashes: set[str] = set(manifest.get("hashes", []))

    entries: List[Dict[str, Any]] = []
    for url in feed_urls:
        try:
            parsed = feedparser.parse(url)
            entries.extend(parsed.entries)
        except Exception as exc:
            logger.warning("解析 RSS 失败: %s | %s", url, exc)

    total_count = len(entries)

    def sort_key(e: Dict[str, Any]):
        if "published_parsed" in e and e.published_parsed:
            try:
                return datetime(*e.published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                logger.warning("Failed to parse published_parsed for sorting: %s", e)
        return datetime.min.replace(tzinfo=ZoneInfo("UTC"))

    entries.sort(key=sort_key, reverse=True)
    entries = entries[:max_articles]

    now_utc = datetime.now(ZoneInfo("UTC"))

    enforce_content_filters = bool(entity_config.get("enforce_content_filters")) if entity_config else False

    for entry in entries:
        url_info = _resolve_entry_url(entry)
        canonical_url = url_info.get("canonical", "")
        raw_url = url_info.get("raw", "")

        current_url = canonical_url or raw_url
        if not current_url:
            skipped_count += 1
            continue

        if not _passes_topic_pre_filters(
            entry=entry,
            canonical_url=current_url,
            topic_config=entity_config,
        ):
            skipped_count += 1
            continue

        if max_age_hours is not None and max_age_hours > 0:
            if _is_entry_too_old(entry, max_age_hours, now_utc):
                skipped_count += 1
                continue

        current_dedupe_hash = _compute_dedupe_hash(entry)
        if current_dedupe_hash in seen_dedupe_hashes and not force:
            skipped_count += 1
            continue

        if raw_url and canonical_url and raw_url != canonical_url:
            logger.debug(
                "Resolved canonical URL for %s: %s -> %s",
                entity_identifier,
                raw_url,
                canonical_url,
            )

        full_text = await _extract_full_text(current_url)
        feed_summary = entry.get("summary", "") or entry.get("description", "")

        if enforce_content_filters:
            if not _passes_topic_content_filters(
                topic_config=entity_config,
                full_text=full_text,
                feed_summary=feed_summary,
            ):
                skipped_count += 1
                continue

        url_hash = _compute_url_hash(current_url)
        article_json = _build_article_json(
            ticker=entity_storage_path,
            date_str=date_str,
            entry=entry,
            full_text=full_text,
            feed_summary=feed_summary,
            url_hash=url_hash,
            logical_ticker=entity_identifier,
            news_type=news_type,
            topic=entity_identifier,
            topic_group=entity_group,
            canonical_url=current_url,
            raw_rss_link=raw_url if raw_url and raw_url != current_url else None,
        )

        try:
            if settings.storage_backend == "gcs":
                rel_path = await _save_gcs_article(
                    article_json,
                    date_str,
                    entity_storage_path,
                    url_hash,
                    base_prefix=effective_prefix,
                )
            else:
                rel_path = _save_local_article(
                    article_json,
                    date_str,
                    entity_storage_path,
                    url_hash,
                    base_prefix=effective_prefix,
                )

            seen_dedupe_hashes.add(current_dedupe_hash)
            manifest.setdefault("hashes", []).append(current_dedupe_hash)
            manifest.setdefault("files", []).append(rel_path)
            new_count += 1
        except Exception as exc:
            logger.error("保存文章失败: %s | %s | %s", entity_storage_path, current_url, exc)
            skipped_count += 1

    if settings.storage_backend == "gcs":
        _save_gcs_manifest(date_str, manifest, base_prefix=effective_prefix)
    else:
        _save_local_manifest(date_str, manifest, base_prefix=effective_prefix)

    return {
        "date_str": date_str,
        "new_count": new_count,
        "skipped_count": skipped_count,
        "total_count": total_count,
    }


async def _crawl_topic(
    *,
    topic_identifier: str,
    topic_storage_path: str,
    topic_group: str,
    news_type: str,
    date_obj: date,
    feed_urls: List[str],
    max_articles: int,
    force: bool = False,
    max_age_hours: Optional[int] = None,
    topic_config: Optional[Dict[str, Any]] = None,
) -> TopicCrawlResult:
    """Generic crawler for topic-oriented news (e.g., macro events)."""

    storage_prefix = (
        (topic_config.get("storage_prefix") if topic_config else None)
        or settings.gcs_topic_news_prefix
    )

    stats = await _crawl_entity(
        entity_identifier=topic_identifier,
        entity_storage_path=topic_storage_path,
        entity_group=topic_group,
        news_type=news_type,
        date_obj=date_obj,
        feed_urls=feed_urls,
        max_articles=max_articles,
        force=force,
        max_age_hours=max_age_hours,
        entity_config=topic_config,
        storage_prefix=storage_prefix,
    )

    return TopicCrawlResult(
        topic=topic_identifier,
        topic_path=topic_storage_path,
        date=stats["date_str"],
        new_count=stats["new_count"],
        skipped_count=stats["skipped_count"],
        total_count=stats["total_count"],
    )


async def _crawl_person(
    *,
    person_identifier: str,
    person_storage_path: str,
    person_group: str,
    news_type: str,
    date_obj: date,
    feed_urls: List[str],
    max_articles: int,
    force: bool = False,
    max_age_hours: Optional[int] = None,
    person_config: Optional[Dict[str, Any]] = None,
) -> PersonCrawlResult:
    """Crawler for person/celebrity news feeds."""

    storage_prefix = (
        (person_config.get("storage_prefix") if person_config else None)
        or settings.gcs_person_news_prefix
    )

    stats = await _crawl_entity(
        entity_identifier=person_identifier,
        entity_storage_path=person_storage_path,
        entity_group=person_group,
        news_type=news_type,
        date_obj=date_obj,
        feed_urls=feed_urls,
        max_articles=max_articles,
        force=force,
        max_age_hours=max_age_hours,
        entity_config=person_config,
        storage_prefix=storage_prefix,
    )

    return PersonCrawlResult(
        person=person_identifier,
        person_path=person_storage_path,
        date=stats["date_str"],
        new_count=stats["new_count"],
        skipped_count=stats["skipped_count"],
        total_count=stats["total_count"],
    )


async def _crawl_topic_dynamic(
    topic_key: str,
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> TopicCrawlResult:
    """Crawl a topic defined dynamically via configuration."""

    cfg = get_topic_config(topic_key)
    if not cfg:
        raise RuntimeError(f"No topic configuration found for '{topic_key}'.")

    feed_urls = cfg.get("rss_sources") or []
    if not feed_urls:
        raise RuntimeError(f"No RSS sources configured for topic '{topic_key}'.")

    topic_identifier = cfg.get("topic_identifier")
    topic_storage_path = cfg.get("topic_storage_path")
    topic_group = cfg.get("topic_group")
    news_type = cfg.get("news_type") or (topic_group or "macro")

    effective_max_articles = max_articles if max_articles is not None else cfg.get("max_articles")
    if not effective_max_articles:
        effective_max_articles = settings.max_articles_per_ticker

    max_age_hours = cfg.get("max_age_hours")
    if max_age_hours is None:
        max_age_hours = settings.news_max_age_hours

    return await _crawl_topic(
        topic_identifier=topic_identifier,
        topic_storage_path=topic_storage_path,
        topic_group=topic_group or "macro",
        news_type=news_type,
        date_obj=date_obj,
        feed_urls=feed_urls,
        max_articles=int(effective_max_articles),
        force=force,
        max_age_hours=max_age_hours,
        topic_config=cfg,
    )


async def _crawl_person_dynamic(
    person_key: str,
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> PersonCrawlResult:
    """Crawl a person defined via persons_config."""

    cfg = get_person_config(person_key)
    if not cfg:
        raise RuntimeError(f"No person configuration found for '{person_key}'.")

    feed_urls = cfg.get("rss_sources") or []
    if not feed_urls:
        raise RuntimeError(f"No RSS sources configured for person '{person_key}'.")

    person_identifier = cfg.get("topic_identifier")
    person_storage_path = cfg.get("topic_storage_path")
    person_group = cfg.get("topic_group") or "celebrity"
    news_type = cfg.get("news_type") or "person"

    effective_max_articles = max_articles if max_articles is not None else cfg.get("max_articles")
    if not effective_max_articles:
        effective_max_articles = settings.max_articles_per_ticker

    max_age_hours = cfg.get("max_age_hours")
    if max_age_hours is None:
        max_age_hours = settings.news_max_age_hours

    return await _crawl_person(
        person_identifier=person_identifier,
        person_storage_path=person_storage_path,
        person_group=person_group,
        news_type=news_type,
        date_obj=date_obj,
        feed_urls=feed_urls,
        max_articles=int(effective_max_articles),
        force=force,
        max_age_hours=max_age_hours,
        person_config=cfg,
    )


async def _crawl_macro_fed_topic(
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> TopicCrawlResult:
    return await _crawl_topic_dynamic(
        "fed_funds_rate",
        date_obj,
        max_articles=max_articles,
        force=force,
    )


async def _crawl_rate_vs_inflation_topic(
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> TopicCrawlResult:
    return await _crawl_topic_dynamic(
        "rate_vs_inflation",
        date_obj,
        max_articles=max_articles,
        force=force,
    )


async def _crawl_rate_heatmap_topic(
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> TopicCrawlResult:
    return await _crawl_topic_dynamic(
        "rate_heatmap",
        date_obj,
        max_articles=max_articles,
        force=force,
    )


async def _crawl_macro_dashboard_topic(
    date_obj: date,
    *,
    max_articles: Optional[int] = None,
    force: bool = False,
) -> TopicCrawlResult:
    return await _crawl_topic_dynamic(
        "macro_dashboard",
        date_obj,
        max_articles=max_articles,
        force=force,
    )

# === ！！！新增工作流函数！！！ ===
async def _process_ticker_for_batch(ticker: str, target_date: date) -> Dict[str, Any]:
    """
    为单个股票执行完整的每日批量处理流程：
    1. 抓取当天最新的新闻 (增量，非强制)。
    2. 加载当天所有已抓取的新闻。
    3. 生成并保存 AI Context。
    """
    logger.info(f"开始为 {ticker} 执行批量处理...")
    try:
        # 步骤 1: 抓取当天最新的新闻 (增量，非强制)
        crawl_result = await _crawl_ticker(
            ticker=ticker,
            date_obj=target_date,
            max_articles=settings.max_articles_per_ticker,
            force=False, # 确保是增量抓取
        )
        logger.info(f"为 {ticker} 抓取完成: 新增 {crawl_result.new_count} 条, 跳过 {crawl_result.skipped_count} 条。")
        
        # 步骤 2: 加载当天所有已抓取的新闻
        articles = await _load_articles_for_ticker_date(target_date, ticker)
        if not articles:
            message = (
                "No articles found after crawling, skipping AI context generation."
            )
            logger.warning(message)
            return {"status": "skipped", "message": message}

        # 步骤 3: 使用新管道生成并保存 AI Context 的各个步骤
        # - 该字段控制“哪几个 step 的 AI Context 需要落盘保存到 ai-context 目录”。
        # - 格式为逗号分隔字符串，示例："2"、"1,2"。
        try:
            # Determine which pipeline steps to output from environment variable
            import os
            steps_env = os.getenv("AI_CONTEXT_OUTPUT_STEPS", "1,2")
            # 解析为整型步骤列表：只接受 {1,2}；去空白、去重、排序
            legal = {1, 2}
            steps_to_output: list[int] = []
            for s in steps_env.split(","):
                s = s.strip()
                if s.isdigit():
                    v = int(s)
                    if v in legal:
                        steps_to_output.append(v)
            
            # 去重并排序
            steps_to_output = sorted(set(steps_to_output))

            if not steps_to_output:
                steps_to_output = [2]

            # Run the pipeline to generate context strings
            from news_crawler.ai_context import (
                _prepare_ai_context_pipeline,
                _save_ai_context_step,
            )

            pipeline_outputs = await _prepare_ai_context_pipeline(
                articles,
                target_date,
                ticker,
                steps_to_output=steps_to_output,
                max_articles_for_context=None,
            )

            if not pipeline_outputs:
                return {
                    "status": "skipped",
                    "message": "No high-quality news to generate context.",
                    "articles_crawled": crawl_result.new_count,
                }

            # Identify the highest step as the final step
            final_step = max(pipeline_outputs.keys())

            saved_paths: Dict[int, str] = {}
            for step_num, context_content in pipeline_outputs.items():
                # Skip diagnostic messages (which start with "No articles"), do not save them
                if context_content.startswith("No articles") or context_content.startswith("No high-quality"):
                    logger.info(
                        f"Step {step_num} for {ticker} on {target_date} produced no usable context."
                    )
                    continue
                # Only update index for the final step
                update_index = bool(step_num == final_step)
                path = await _save_ai_context_step(
                    target_date, ticker, context_content, step_num, update_index=update_index
                )
                saved_paths[step_num] = path

            if not saved_paths:
                return {
                    "status": "skipped",
                    "message": "No high-quality news to generate context.",
                    "articles_crawled": crawl_result.new_count,
                }

            # Return the path of the final step and include all saved step paths for reference
            result: Dict[str, Any] = {
                "status": "success",
                "articles_crawled": crawl_result.new_count,
                "total_articles_for_context": len(articles),
                "saved_steps": saved_paths,
            }
            if final_step in saved_paths:
                # Use a common key for final output
                result["ai_context_path"] = saved_paths[final_step]
            return result
        except Exception as e:
            logger.error(
                f"Failed to generate AI context pipeline for {ticker} on {target_date}: {e}",
                exc_info=True,
            )
            return {
                "status": "failed",
                "error": str(e),
                "articles_crawled": crawl_result.new_count,
            }

    except Exception as e:
        logger.error(f"为 {ticker} 执行批量处理时出错: {e}", exc_info=True)


async def _process_topic_for_batch(topic_key: str, target_date: date) -> Dict[str, Any]:
    """Generic batch processor for a topic identified by topic_key using dynamic config."""

    cfg = get_topic_config(topic_key)
    if not cfg:
        message = f"No topic configuration found for '{topic_key}'"
        logger.warning(message)
        return {"status": "failed", "message": message}

    canonical_key = cfg.get("key") or topic_key.strip().lower()
    topic_key_norm = canonical_key
    logger.info(
        f"Starting batch processing for topic_key={canonical_key} date={target_date}"
    )

    try:
        crawl_result = await _crawl_topic_dynamic(canonical_key, target_date, max_articles=None, force=False)
    except RuntimeError as err:
        message = str(err)
        logger.warning(f"Crawl failed for topic_key={canonical_key}: {message}")
        return {"status": "failed", "message": message}

    topic_identifier = cfg.get("topic_identifier")
    topic_storage_path = cfg.get("topic_storage_path")
    max_articles = cfg.get("max_articles_for_context") or cfg.get("max_articles")
    news_type = cfg.get("news_type") or (cfg.get("topic_group") or "macro")
    storage_prefix = cfg.get("storage_prefix") or settings.gcs_topic_news_prefix

    articles = await _load_articles_for_ticker_date(
        target_date,
        topic_storage_path,
        base_prefix=storage_prefix,
    )
    if not articles:
        message = f"No articles found for topic {topic_identifier} (path={topic_storage_path}) after crawling, skipping AI context generation."
        logger.warning(message)
        return {"status": "skipped", "message": message, "articles_crawled": crawl_result.new_count}

    try:
        import os
        steps_env = os.getenv("AI_CONTEXT_OUTPUT_STEPS", "1,2")
        legal = {1, 2}
        steps_to_output: List[int] = []
        for s in steps_env.split(","):
            s = s.strip()
            if s.isdigit():
                v = int(s)
                if v in legal:
                    steps_to_output.append(v)
        steps_to_output = sorted(set(steps_to_output)) or [2]

        pipeline_outputs = await _prepare_ai_context_pipeline(
            articles,
            target_date,
            topic_identifier,
            steps_to_output=steps_to_output,
            max_articles_for_context=max_articles,
        )

        if not pipeline_outputs:
            return {"status": "skipped", "message": "No high-quality macro news to generate context.", "articles_crawled": crawl_result.new_count}

        final_step = max(pipeline_outputs.keys())
        saved_paths: Dict[int, str] = {}
        for step_num, context_content in pipeline_outputs.items():
            if context_content.startswith("No articles") or context_content.startswith("No high-quality"):
                logger.info(f"Step {step_num} for {topic_identifier} on {target_date} produced no usable context.")
                continue
            update_index = bool(step_num == final_step)
            path = await _save_ai_context_step(
                target_date,
                topic_identifier,
                context_content,
                step_num,
                update_index=update_index,
                preserve_case=True,
                index_extra_fields={
                    "news_type": news_type,
                    "topic": topic_identifier,
                    "topic_path": topic_storage_path,
                },
            )
            saved_paths[step_num] = path

        if not saved_paths:
            return {"status": "skipped", "message": "No high-quality macro news to generate context.", "articles_crawled": crawl_result.new_count}

        result: Dict[str, Any] = {
            "status": "success",
            "articles_crawled": crawl_result.new_count,
            "total_articles_for_context": len(articles),
            "saved_steps": saved_paths,
        }
        if final_step in saved_paths:
            result["ai_context_path"] = saved_paths[final_step]
        logger.info(f"Batch processing complete for topic_key={topic_key_norm} date={target_date} new_articles={crawl_result.new_count}")
        return result
    except Exception as e:
        logger.error(f"Failed to generate AI context pipeline for {topic_identifier} on {target_date}: {e}", exc_info=True)
        return {"status": "failed", "error": str(e), "articles_crawled": crawl_result.new_count}


async def _process_person_for_batch(person_key: str, target_date: date) -> Dict[str, Any]:
    """Batch processor for a person entry defined in persons_config."""

    cfg = get_person_config(person_key)
    if not cfg:
        message = f"No person configuration found for '{person_key}'"
        logger.warning(message)
        return {"status": "failed", "message": message}

    canonical_key = cfg.get("key") or person_key.strip().lower()
    logger.info("Starting batch processing for person_key=%s date=%s", canonical_key, target_date)

    try:
        crawl_result = await _crawl_person_dynamic(canonical_key, target_date, max_articles=None, force=False)
    except RuntimeError as err:
        message = str(err)
        logger.warning("Crawl failed for person_key=%s: %s", canonical_key, message)
        return {"status": "failed", "message": message}

    person_identifier = cfg.get("topic_identifier")
    person_storage_path = cfg.get("topic_storage_path")
    max_articles = cfg.get("max_articles_for_context") or cfg.get("max_articles")
    news_type = cfg.get("news_type") or "person"
    storage_prefix = cfg.get("storage_prefix") or settings.gcs_person_news_prefix

    articles = await _load_articles_for_ticker_date(
        target_date,
        person_storage_path,
        base_prefix=storage_prefix,
    )
    if not articles:
        message = (
            f"No articles found for person {person_identifier} (path={person_storage_path}) after crawling,"
            " skipping AI context generation."
        )
        logger.warning(message)
        return {"status": "skipped", "message": message, "articles_crawled": crawl_result.new_count}

    try:
        import os
        steps_env = os.getenv("AI_CONTEXT_OUTPUT_STEPS", "1,2")
        legal = {1, 2}
        steps_to_output: List[int] = []
        for s in steps_env.split(","):
            s = s.strip()
            if s.isdigit():
                v = int(s)
                if v in legal:
                    steps_to_output.append(v)
        steps_to_output = sorted(set(steps_to_output)) or [2]

        pipeline_outputs = await _prepare_ai_context_pipeline(
            articles,
            target_date,
            person_identifier,
            steps_to_output=steps_to_output,
            max_articles_for_context=max_articles,
        )

        if not pipeline_outputs:
            return {
                "status": "skipped",
                "message": "No high-quality person news to generate context.",
                "articles_crawled": crawl_result.new_count,
            }

        final_step = max(pipeline_outputs.keys())
        saved_paths: Dict[int, str] = {}
        for step_num, context_content in pipeline_outputs.items():
            if context_content.startswith("No articles") or context_content.startswith("No high-quality"):
                logger.info("Step %s for %s on %s produced no usable context.", step_num, person_identifier, target_date)
                continue
            update_index = bool(step_num == final_step)
            path = await _save_ai_context_step(
                target_date,
                person_identifier,
                context_content,
                step_num,
                update_index=update_index,
                preserve_case=True,
                index_extra_fields={
                    "news_type": news_type,
                    "person": person_identifier,
                    "person_path": person_storage_path,
                },
            )
            saved_paths[step_num] = path

        if not saved_paths:
            return {
                "status": "skipped",
                "message": "No high-quality person news to generate context.",
                "articles_crawled": crawl_result.new_count,
            }

        result: Dict[str, Any] = {
            "status": "success",
            "articles_crawled": crawl_result.new_count,
            "total_articles_for_context": len(articles),
            "saved_steps": saved_paths,
        }
        if final_step in saved_paths:
            result["ai_context_path"] = saved_paths[final_step]
        logger.info(
            "Batch processing complete for person_key=%s date=%s new_articles=%s",
            canonical_key,
            target_date,
            crawl_result.new_count,
        )
        return result
    except Exception as exc:
        logger.error(
            "Failed to generate AI context pipeline for %s on %s: %s",
            person_identifier,
            target_date,
            exc,
            exc_info=True,
        )
        return {"status": "failed", "error": str(exc), "articles_crawled": crawl_result.new_count}


async def diagnose_rss_sources(
    *,
    topic_keys: Optional[List[str]] = None,
    max_entries_per_source: int = 3,
    enable_debug_log: bool = False,
    get_config = get_topic_config,
    get_all_configs = get_all_topic_configs,
    registry_label: str = "topic",
) -> List[RSSSourceDiagnostic]:
    """Run crawl diagnostics for configured RSS sources and return detailed results."""

    if max_entries_per_source <= 0:
        raise ValueError("max_entries_per_source must be greater than zero")

    previous_level = logger.level
    if enable_debug_log:
        logger.info("Enabling DEBUG log level for RSS diagnostics run")
        logger.setLevel(logging.DEBUG)

    try:
        configs_map = get_all_configs()
        selected_keys: List[str]
        if topic_keys:
            normalized_keys: set[str] = set()
            for raw_key in topic_keys:
                cfg = get_config(raw_key)
                if not cfg:
                    logger.warning(
                        "Skipping unknown %s key during diagnostics: %s",
                        registry_label,
                        raw_key,
                    )
                    continue
                normalized_keys.add(cfg.get("key") or raw_key.strip().lower())
            selected_keys = [key for key in configs_map if key in normalized_keys]
        else:
            selected_keys = list(configs_map.keys())

        diagnostics: List[RSSSourceDiagnostic] = []

        for topic_key in selected_keys:
            cfg = configs_map.get(topic_key)
            if not cfg:
                logger.warning(
                    "%s config unexpectedly missing during diagnostics: %s",
                    registry_label,
                    topic_key,
                )
                continue

            topic_identifier = cfg.get("topic_identifier")
            feed_urls: List[str] = cfg.get("rss_sources") or []
            if not feed_urls:
                diag = RSSSourceDiagnostic(
                    topic_key=topic_key,
                    topic_identifier=topic_identifier,
                    rss_url="",
                    fetch_ok=False,
                    entry_count=0,
                    errors=["no_rss_sources_configured"],
                    entries=[],
                )
                diagnostics.append(diag)
                continue

            for rss_url in feed_urls:
                diag = RSSSourceDiagnostic(
                    topic_key=topic_key,
                    topic_identifier=topic_identifier,
                    rss_url=rss_url,
                    fetch_ok=False,
                    entry_count=0,
                )
                logger.info(
                    "Diagnosing RSS source | %s=%s identifier=%s url=%s",
                    registry_label,
                    topic_key,
                    topic_identifier,
                    rss_url,
                )

                try:
                    parsed = feedparser.parse(rss_url)
                except Exception as exc:
                    error_msg = f"parse_error: {exc}"
                    logger.error(
                        "Failed to parse RSS feed during diagnostics | %s=%s url=%s error=%s",
                        registry_label,
                        topic_key,
                        rss_url,
                        exc,
                    )
                    diag.errors.append(error_msg)
                    diagnostics.append(diag)
                    continue

                diag.fetch_ok = True
                diag.entry_count = len(getattr(parsed, "entries", []) or [])

                if getattr(parsed, "bozo", False):
                    bozo_exc = getattr(parsed, "bozo_exception", None)
                    if bozo_exc:
                        diag.errors.append(f"bozo_exception: {bozo_exc}")
                        logger.warning(
                            "RSS feed contains parse anomalies | %s=%s url=%s error=%s",
                            registry_label,
                            topic_key,
                            rss_url,
                            bozo_exc,
                        )

                limited_entries = (getattr(parsed, "entries", []) or [])[:max_entries_per_source]

                for entry in limited_entries:
                    url_info = _resolve_entry_url(entry)
                    canonical_url = url_info.get("canonical", "")
                    raw_url = url_info.get("raw", "")
                    target_url = canonical_url or raw_url

                    pre_ok, pre_reasons = _diagnose_topic_pre_filters(
                        entry=entry,
                        canonical_url=target_url,
                        topic_config=cfg,
                    )

                    feed_summary = entry.get("summary") or entry.get("description")
                    full_text: Optional[str] = None
                    if target_url:
                        full_text = await _extract_full_text(target_url)

                    content_ok, content_reasons, full_len, summary_len = _diagnose_topic_content_constraints(
                        topic_config=cfg,
                        full_text=full_text,
                        feed_summary=feed_summary,
                    )

                    rejection_reasons: List[str] = []
                    if not pre_ok:
                        rejection_reasons.extend(pre_reasons)
                    if pre_ok and not content_ok:
                        rejection_reasons.extend(content_reasons)

                    diag_entry = RSSSourceEntryDiagnostic(
                        title=(entry.get("title") or "").strip(),
                        published=entry.get("published"),
                        raw_url=raw_url,
                        canonical_url=target_url,
                        full_text_ok=bool(full_text),
                        full_text_length=full_len,
                        summary_length=summary_len,
                        passes_pre_filters=pre_ok,
                        passes_content_filters=content_ok,
                        rejection_reasons=rejection_reasons,
                    )
                    diag.entries.append(diag_entry)

                    logger.info(
                        "Diagnostics result | %s=%s url=%s title=%s pre_ok=%s content_ok=%s full_text_len=%s reasons=%s",
                        registry_label,
                        topic_key,
                        rss_url,
                        diag_entry.title,
                        pre_ok,
                        content_ok,
                        full_len,
                        ",".join(rejection_reasons) or "none",
                    )

                diagnostics.append(diag)

        return diagnostics
    finally:
        if enable_debug_log:
            logger.info("Restoring previous log level after RSS diagnostics")
            logger.setLevel(previous_level)
