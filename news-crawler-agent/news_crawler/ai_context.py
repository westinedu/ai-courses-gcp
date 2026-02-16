# news_crawler/ai_context.py
import json
import logging
import os
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from settings import settings # *** 注意：这里导入 settings 的路径已修改 ***
from news_crawler.storage import (
    gcs_bucket,  # gcs_bucket 和 _get_date_dir
    _list_gcs_objects, _list_local_objects, # *** 现在可以正确导入这两个函数 ***
    _load_daily_ai_context_index, _save_daily_ai_context_index, _append_daily_ai_context_index # 索引相关函数
)
from news_crawler.utils import _parse_date,_get_date_dir # *** 导入 _parse_date，因为 _get_latest_ai_context_path_common_logic 需要它 ***
from fastapi import HTTPException # 导入 HTTPException，因为 _get_latest_ai_context_path_common_logic 会抛出它
from news_crawler.dynamic_config import get_topic_config

logger = logging.getLogger("news_crawler_agent")


DEFAULT_STEP2_HIGHLIGHT_KEYWORDS: List[str] = [
    "federal reserve",
    "fed",
    "interest rate",
    "rate cut",
    "rates",
    "fomc",
    "inflation",
    "tariff",
    "treasury",
    "yield",
    "economic",
    "growth",
    "policy",
]


def _resolve_highlight_keywords(topic_identifier: str) -> List[str]:
    cfg = get_topic_config(topic_identifier)
    if cfg:
        keywords = cfg.get("highlight_keywords") or []
        if keywords:
            return keywords
    return []

async def _load_articles_for_ticker_date(
    target_date: date,
    ticker: str,
    *,
    base_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    加载指定日期和股票的所有原始新闻文章 JSON 数据。
    """
    date_str = _get_date_dir(target_date)
    articles: List[Dict[str, Any]] = []
    storage_prefix = base_prefix or settings.gcs_base_prefix
    
    try:    
        if settings.storage_backend == "gcs":
            path_info = _list_gcs_objects(date_str, ticker, base_prefix=storage_prefix) # _list_gcs_objects现在排除了AI Context文件
            prefix = path_info.get("prefix", "")
            object_names = path_info.get("objects", [])
            
            for obj_rel_path in object_names:
                full_object_name = os.path.join(prefix, obj_rel_path)
                blob = gcs_bucket.blob(full_object_name)
                try:
                    content = blob.download_as_text(encoding="utf-8")
                    articles.append(json.loads(content))
                except Exception as e:
                    logger.warning(f"无法从 GCS 加载文章 {full_object_name}: {e}")
        else: # local storage
            path_info = _list_local_objects(date_str, ticker, base_prefix=storage_prefix)
            
            full_date_path_in_storage = os.path.join(settings.local_storage_root, storage_prefix, date_str)

            object_paths_relative_to_date_dir = path_info.get("objects", [])
            for obj_rel_path in object_paths_relative_to_date_dir:
                full_file_path = os.path.join(full_date_path_in_storage, obj_rel_path)
                try:
                    with open(full_file_path, "r", encoding="utf-8") as f:
                        articles.append(json.load(f))
                except Exception as e:
                    logger.warning(f"无法从本地加载文章 {full_file_path}: {e}")
    except Exception as e:
        logger.error(f"加载 {ticker} 在 {date_str} 的文章列表失败: {e}", exc_info=True)
    
    return articles

def _prepare_ai_context_for_articles(articles: List[Dict[str, Any]], target_date: date, ticker: str, max_articles_for_context: Optional[int] = None) -> str:
    """
    根据提供的文章列表，生成适合AI输入的新闻上下文。
    优先选择有完整正文的文章，并进行格式化。
    如果 max_articles_for_context 为 None，则包含所有符合质量要求的文章。
    """
    """
    This function generates an AI context by concatenating the raw news articles for a ticker on a
    specific date. It represents **Step 1** of the AI context processing pipeline.  In this step
    we include articles that either have a complete extracted body (``fulltext_ok``) with a
    reasonable length or at least a non‐trivial summary.  For articles with a full text, we
    include the entire content; otherwise we fall back to their summary.  The resulting
    context contains headers, article metadata and the selected content for each article.

    .. note::
        This helper now delegates its implementation to the new pipeline step helper
        ``_prepare_ai_context_step1``.  It is kept for backwards compatibility.
    """
    # Delegate to the pipeline step1 implementation.  This preserves existing behaviour while
    # centralising the logic for step 1 in ``_prepare_ai_context_step1``.
    return _prepare_ai_context_step1(articles, target_date, ticker, max_articles_for_context)


def _prepare_ai_context_step1(
    articles: List[Dict[str, Any]],
    target_date: date,
    ticker: str,
    max_articles_for_context: Optional[int] = None,
) -> str:
    """
    Construct the Step 1 AI context by concatenating raw news articles.

    This step mirrors the original ``_prepare_ai_context_for_articles`` implementation.  It
    selects articles that either have a valid full text with sufficient length or a non-empty
    summary.  Articles are sorted with full-text articles first, then by content length and
    publication time.  The generated context includes a header identifying the ticker and
    date, the generation timestamp in the configured timezone, and each article's title,
    source, publication date, URL and the chosen content (full text if available, otherwise
    the summary).

    Parameters
    ----------
    articles : list
        List of article JSON objects loaded from storage.
    target_date : date
        The target date for which the context is being generated.
    ticker : str
        Stock ticker associated with the articles.
    max_articles_for_context : int, optional
        Maximum number of articles to include in the context; if ``None`` all eligible
        articles are included.

    Returns
    -------
    str
        The concatenated AI context for Step 1.  If no articles meet the quality criteria,
        returns a diagnostic message.
    """
    # Filter for quality: prefer full-text articles but include summaries if sufficiently long
    filtered_articles: List[Dict[str, Any]] = [
        a
        for a in articles
        if (
            a.get("extraction", {}).get("fulltext_ok")
            and a.get("metrics", {}).get("content_len", 0) > 50
        )
        or (
            a.get("extraction", {}).get("summary")
            and len(a.get("extraction", {}).get("summary").strip()) > 20
        )
    ]

    # Sort articles: full-text first, then by content length, then by publication time (UTC)
    sorted_articles = sorted(
        filtered_articles,
        key=lambda x: (
            x.get("extraction", {}).get("fulltext_ok", False),
            x.get("metrics", {}).get("content_len", 0),
            datetime.fromisoformat(x.get("published"))
            .astimezone(ZoneInfo("UTC"))
            if x.get("published")
            else datetime.min.replace(tzinfo=ZoneInfo("UTC")),
        ),
        reverse=True,
    )

    if max_articles_for_context is not None and max_articles_for_context > 0:
        sorted_articles = sorted_articles[: max_articles_for_context]

    if not sorted_articles:
        return (
            "No high-quality news articles found to generate AI context for this date and ticker."
        )

    context_blocks: List[str] = []
    # Include step annotation in the header
    context_datetime_local = datetime.now(ZoneInfo(settings.timezone))
    context_blocks.append(
        f"--- News AI Context for {ticker.upper()} on {_get_date_dir(target_date)} ---"
    )
    context_blocks.append(
        f"Generated at (Local {settings.timezone}): {context_datetime_local.isoformat()}"
    )
    context_blocks.append(
        "Step 1: Raw news concatenation. This step concatenates the raw news articles "
        "(using full text when available, otherwise the summary)."
    )
    context_blocks.append("")

    # Build blocks for each article
    for i, article in enumerate(sorted_articles):
        title = article.get("title", "N/A").strip()
        source = article.get("source", "Unknown Source").strip()
        published = article.get("published", "N/A")
        url = article.get("url", "N/A")

        content_to_use: str = ""
        if article.get("extraction", {}).get("fulltext_ok") and article.get("extraction", {}).get(
            "content"
        ):
            content_to_use = article.get("extraction", {}).get("content")
        elif article.get("extraction", {}).get("summary"):
            content_to_use = article.get("extraction", {}).get("summary")

        if not content_to_use.strip():
            continue

        block = (
            f"--- Article {i+1} ---\n"
            f"Title: {title}\n"
            f"Source: {source}\n"
            f"Published Date: {published}\n"
            f"URL: {url}\n"
            f"Content:\n{content_to_use}\n"
        )
        context_blocks.append(block)

    return "\n\n".join(context_blocks)


def _extract_sentences(text: str) -> List[str]:
    if not text:
        return []
    sentences = re.split(r"(?<=[。.!?？!])\s+", text)
    return [s.strip() for s in sentences if s and s.strip()]


def _select_highlight_sentences(
    sentences: List[str],
    keywords: List[str],
    *,
    exclude_lower: Optional[Set[str]] = None,
    max_sentences: int = 3,
) -> List[str]:
    scored: List[tuple[int, int, str]] = []
    for idx, sent in enumerate(sentences):
        lower_sent = sent.lower()
        if exclude_lower and lower_sent in exclude_lower:
            continue
        score = 0
        for kw in keywords:
            if kw and kw in lower_sent:
                score += 3
        if re.search(r"\d", sent):
            score += 1
        if score:
            scored.append((score, idx, sent))
    if not scored:
        # fallback to the leading sentences if nothing matched
        fallback = sentences[:max_sentences]
        return fallback
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: List[tuple[int, str]] = []
    for score, idx, sent in scored:
        if len(selected) >= max_sentences:
            break
        selected.append((idx, sent))
    selected.sort(key=lambda item: item[0])
    return [sent for _, sent in selected]


def _build_step2_content(
    article: Dict[str, Any],
    *,
    keywords: Optional[List[str]] = None,
    max_chars: int = 1200,
    max_highlight_sentences: int = 3,
) -> str:
    extraction = article.get("extraction", {}) or {}
    summary = (extraction.get("summary") or "").strip()
    full_text = (extraction.get("content") or "").strip()

    if not summary:
        sentences = _extract_sentences(full_text)
        if sentences:
            summary = " ".join(sentences[:3])
        else:
            summary = full_text[:300]
        summary = (summary or "").strip()

    highlight_text = ""
    if full_text:
        sentences = _extract_sentences(full_text)
        highlight_keywords = keywords or DEFAULT_STEP2_HIGHLIGHT_KEYWORDS
        summary_sentences = _extract_sentences(summary)
        summary_lower_set: Set[str] = {s.lower() for s in summary_sentences}
        highlights = _select_highlight_sentences(
            sentences,
            highlight_keywords,
            exclude_lower=summary_lower_set,
            max_sentences=max_highlight_sentences,
        )
        if highlights:
            highlight_text = "Highlights: " + " ".join(highlights)

    parts: List[str] = []
    if summary:
        parts.append(summary)
    if highlight_text:
        parts.append(highlight_text)

    if not parts:
        return ""

    condensed = "\n".join(parts).strip()
    if len(condensed) > max_chars:
        truncated = condensed[:max_chars]
        condensed = truncated.rsplit(" ", 1)[0].rstrip() + "..."
    return condensed


def _prepare_ai_context_step2(
    articles: List[Dict[str, Any]],
    target_date: date,
    ticker: str,
    max_articles_for_context: Optional[int] = None,
) -> str:
    """
    Construct the Step 2 AI context by filtering out low-quality articles and
    compressing the remaining articles to their summaries.

    In this step we remove all articles for which ``fulltext_ok`` is ``False``, as such
    articles lack a complete extracted body and may not provide enough information for an
    LLM to make accurate judgements.  For the remaining articles (where ``fulltext_ok``
    is ``True``), we include only their summary instead of the full text.  This reduces
    token consumption while preserving the key points of the article.  Articles are
    sorted by content length and publication time to prioritise longer, more informative
    pieces.

    Parameters
    ----------
    articles : list
        Original list of article JSON objects.
    target_date : date
        The target date for which the context is being generated.
    ticker : str
        Stock ticker associated with the articles.
    max_articles_for_context : int, optional
        Maximum number of articles to include; if ``None`` includes all eligible
        articles.

    Returns
    -------
    str
        The AI context for Step 2.  If no articles meet the criteria, returns a
        diagnostic message.
    """
    # Filter to only articles with a valid full text
    filtered_articles: List[Dict[str, Any]] = [
        a
        for a in articles
        if a.get("extraction", {}).get("fulltext_ok")
        and a.get("extraction", {}).get("content")
    ]

    # Sort articles by content length and publication time (descending)
    sorted_articles = sorted(
        filtered_articles,
        key=lambda x: (
            x.get("metrics", {}).get("content_len", 0),
            datetime.fromisoformat(x.get("published"))
            .astimezone(ZoneInfo("UTC"))
            if x.get("published")
            else datetime.min.replace(tzinfo=ZoneInfo("UTC")),
        ),
        reverse=True,
    )

    if max_articles_for_context is not None and max_articles_for_context > 0:
        sorted_articles = sorted_articles[: max_articles_for_context]

    if not sorted_articles:
        return (
            "No articles with full text available to generate Step 2 AI context for this date and ticker."
        )

    context_blocks: List[str] = []
    context_datetime_local = datetime.now(ZoneInfo(settings.timezone))
    context_blocks.append(
        f"--- News AI Context for {ticker.upper()} on {_get_date_dir(target_date)} ---"
    )
    context_blocks.append(
        f"Generated at (Local {settings.timezone}): {context_datetime_local.isoformat()}"
    )
    context_blocks.append(
        "Step 2: Filtered and summarised news. This step removes articles without full text "
        "and uses only the summary of the remaining articles."
    )
    context_blocks.append("")

    topic_highlight_keywords = _resolve_highlight_keywords(ticker)

    for i, article in enumerate(sorted_articles):
        title = article.get("title", "N/A").strip()
        source = article.get("source", "Unknown Source").strip()
        published = article.get("published", "N/A")
        url = article.get("url", "N/A")

        summary = _build_step2_content(
            article,
            keywords=topic_highlight_keywords,
        )

        block = (
            f"--- Article {i+1} ---\n"
            f"Title: {title}\n"
            f"Source: {source}\n"
            f"Published Date: {published}\n"
            f"URL: {url}\n"
            f"Summary:\n{summary}\n"
        )
        context_blocks.append(block)

    return "\n\n".join(context_blocks)


async def _prepare_ai_context_pipeline(
    articles: List[Dict[str, Any]],
    target_date: date,
    ticker: str,
    steps_to_output: Optional[List[int]] = None,
    max_articles_for_context: Optional[int] = None,
) -> Dict[int, str]:
    """
    Run the AI context preparation pipeline for the given articles and return the context
    strings for each step.

    Parameters
    ----------
    articles : list
        List of article JSON objects loaded from storage.
    target_date : date
        Target date for context generation.
    ticker : str
        Stock ticker associated with the articles.
    steps_to_output : list of int, optional
        List of step numbers whose outputs should be returned.  If ``None`` (default)
        all available steps will be returned.  Currently available steps are ``1`` and
        ``2``.  This parameter has no effect on the internal processing order; it only
        determines which steps are included in the returned dictionary.
    max_articles_for_context : int, optional
        Maximum number of articles to consider for each step.  If ``None`` the default
        behaviour for each step is used.

    Returns
    -------
    dict
        A mapping from step number to the corresponding context string.
    """
    # Determine which steps to run
    available_steps = {1, 2}
    steps_to_run: List[int]
    if steps_to_output is None:
        steps_to_run = sorted(list(available_steps))
    else:
        # Filter out invalid steps silently
        steps_to_run = [s for s in steps_to_output if s in available_steps]
        if not steps_to_run:
            steps_to_run = sorted(list(available_steps))

    outputs: Dict[int, str] = {}

    # Step 1
    if 1 in steps_to_run:
        context1 = _prepare_ai_context_step1(
            articles, target_date, ticker, max_articles_for_context
        )
        outputs[1] = context1

    # Step 2: uses the same list of articles but applies its own filtering
    if 2 in steps_to_run:
        context2 = _prepare_ai_context_step2(
            articles, target_date, ticker, max_articles_for_context
        )
        outputs[2] = context2

    return outputs


async def _save_ai_context_step(
    date_obj: date,
    ticker: str,
    context_content: str,
    step_num: int,
    update_index: bool = False,
    *,
    preserve_case: bool = False,
    index_extra_fields: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Save the AI context for a specific pipeline step to storage (GCS or local).

    Parameters
    ----------
    date_obj : date
        Date for which the context is generated.
    ticker : str
        Stock ticker for which the context is generated.
    context_content : str
        The content to save.
    step_num : int
        The step number (used in the filename to indicate intermediate or final output).
    update_index : bool, default ``False``
        Whether to append this context's path to the daily index.  For intermediate
        steps this should remain ``False``; for the final step it should be set to
        ``True`` so that downstream services can discover the latest AI context.

    Returns
    -------
    str
        The path to the saved context.  In ``gcs`` mode this is a ``gs://...`` URI;
        in ``local`` mode it is the absolute filesystem path.
    """
    ticker_clean = ticker.strip()
    ticker_norm = ticker_clean if preserve_case else ticker_clean.upper()
    date_str = _get_date_dir(date_obj)
    now_utc_for_filename = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%d%H%M%S")
    filename = f"{ticker_norm}_step{step_num}_{now_utc_for_filename}_UTC.txt"

    # Choose storage backend
    if settings.storage_backend == "gcs":
        if gcs_bucket is None:
            raise RuntimeError("GCS client not initialized. Cannot save AI context.")
        object_name = f"{settings.gcs_ai_context_prefix}/{date_str}/{filename}"
        blob = gcs_bucket.blob(object_name)
        try:
            blob.upload_from_string(
                context_content, content_type="text/plain; charset=utf-8"
            )
            gcs_full_path = f"gs://{settings.gcs_bucket_name}/{object_name}"
            logger.info(
                f"AI context (step {step_num}) for {ticker_norm} on {date_str} saved to GCS: {gcs_full_path}"
            )
            if update_index:
                # Update the daily index to only include the final step output
                await _append_daily_ai_context_index(
                    date_obj,
                    ticker_norm,
                    gcs_full_path,
                    preserve_case=preserve_case,
                    extra_fields=index_extra_fields,
                )
            return gcs_full_path
        except Exception as e:
            logger.error(
                f"保存 AI context (step {step_num}) 到 GCS 失败: {object_name} | {e}",
                exc_info=True,
            )
            raise
    else:
        # local storage backend: save to filesystem under ai-context prefix
        base_dir = os.path.join(
            settings.local_storage_root, settings.gcs_ai_context_prefix, date_str
        )
        os.makedirs(base_dir, exist_ok=True)
        full_path = os.path.join(base_dir, filename)
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(context_content)
            logger.info(
                f"AI context (step {step_num}) for {ticker_norm} on {date_str} saved locally: {full_path}"
            )
            # When using local storage we do not maintain a GCS index.  Downstream services
            # will need to read local files directly.
            return full_path
        except Exception as e:
            logger.error(
                f"保存 AI context (step {step_num}) 到本地文件失败: {full_path} | {e}",
                exc_info=True,
            )
            raise


async def _save_ai_context_to_gcs(date_obj: date, ticker: str, context_content: str) -> str:
    """
    将 AI context 文本内容保存到 GCS 的 ai_context 目录下。
    并在成功保存后更新每日 AI Context 索引。
    """
    if settings.storage_backend != "gcs":
        logger.warning("STORAGE_BACKEND is not 'gcs'. AI context will not be saved to GCS.")
        return "Not saved to GCS (local storage backend)."
    
    if gcs_bucket is None:
        raise RuntimeError("GCS client not initialized. Cannot save AI context.")

    date_str = _get_date_dir(date_obj)
    now_utc_for_filename = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%d%H%M%S")
    filename = f"{ticker.upper()}_{now_utc_for_filename}_UTC.txt"
    
    object_name = f"{settings.gcs_ai_context_prefix}/{date_str}/{filename}"
    blob = gcs_bucket.blob(object_name)

    try:
        blob.upload_from_string(context_content, content_type="text/plain; charset=utf-8")
        gcs_full_path = f"gs://{settings.gcs_bucket_name}/{object_name}"
        logger.info(f"AI context for {ticker} on {date_str} saved to GCS: {gcs_full_path}")
        
        await _append_daily_ai_context_index(date_obj, ticker, gcs_full_path)
        
        return gcs_full_path
    except Exception as e:
        logger.error(f"保存 AI context 到 GCS 失败: {object_name} | {e}", exc_info=True)
        raise

# === ！！！将 _get_latest_ai_context_path_common_logic 移到这里！！！ ===
async def _get_latest_ai_context_path_common_logic(ticker: str, target_date: date) -> Dict[str, Any]:
    ticker_norm = ticker.strip().upper()
    
    # _load_daily_ai_context_index 已从 storage.py 导入
    index_list = _load_daily_ai_context_index(target_date)
    
    # 筛选出指定股票的条目
    ticker_contexts = [item for item in index_list if item.get("ticker") == ticker_norm]
    
    if not ticker_contexts:
        raise HTTPException(status_code=404, detail=f"No AI Context found for {ticker_norm} on {_get_date_dir(target_date)}.")
    
    # 因为 _append_daily_ai_context_index 已经保证了索引是按时间戳降序排列的，
    # 所以第一个条目就是最新的
    latest_context = ticker_contexts[0]
    
    return {
        "ticker": ticker_norm,
        "date": _get_date_dir(target_date),
        "path": latest_context.get("path"),
        "generated_at": latest_context.get("timestamp") # 提供生成时间
    }
