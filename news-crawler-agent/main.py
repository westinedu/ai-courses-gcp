# main.py
import io
import zipfile
import logging
import os  # Needed for path operations in download and local storage functions
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Body, Path
from starlette.responses import StreamingResponse

# 从根目录导入 settings
from settings import settings

# 从 news_crawler 模块导入 Pydantic 模型
from news_crawler.models import (
    CrawlResult,
    CrawlDailyRequest,
    TickerRequest,
    TopicRequest,
    TopicCrawlResult,
    PersonRequest,
    PersonCrawlResult,
    GenerateDailyAIContextRequest,
    GenerateTickerAIContextRequest,
    RSSDiagnosticsRequest,
    RSSDiagnosticsResponse,
)
# 从 news_crawler 模块导入通用工具函数
from news_crawler.utils import _parse_date, _get_date_dir # *** 从 utils 导入 _parse_date 和 _get_date_dir ***

# 从 news_crawler 模块导入核心逻辑
from news_crawler.core import (
    _crawl_ticker,
    _process_ticker_for_batch,
    _crawl_topic_dynamic,
    _process_person_for_batch,
    _crawl_person_dynamic,
    _process_topic_for_batch,
    diagnose_rss_sources,
)

# 从 news_crawler 模块导入 AI Context 逻辑
from news_crawler.ai_context import (
    _load_articles_for_ticker_date,
    _prepare_ai_context_for_articles,
    _save_ai_context_to_gcs,
    _get_latest_ai_context_path_common_logic,
    _prepare_ai_context_pipeline,
    _save_ai_context_step,
)

# 从 news_crawler 模块导入存储逻辑
from news_crawler.storage import (
    _list_gcs_objects, _list_local_objects, gcs_bucket,
    _load_daily_ai_context_index
)

# Dynamic topic config refresh
from news_crawler.dynamic_config import refresh as refresh_topic_configs
from news_crawler.persons_config import (
    refresh as refresh_person_configs,
    get_person_config,
    get_all_person_configs,
)

# 配置日志输出
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("news_crawler_agent")

app = FastAPI(title=settings.app_name)

# --- Crawl API Routes ---

@app.post("/crawl/daily", response_model=List[CrawlResult])
async def crawl_daily_api(
    body: CrawlDailyRequest = Body(..., description="批量抓取请求体，以 JSON 形式提交。"),
):
    """
    **主要行为:** 为所有默认关注的股票（或指定列表）执行每日批量新闻抓取。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 RSS 源获取指定日期（或今天）的最新新闻，并尝试抽取正文。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。新抓取的新闻文章将以 JSON 格式保存到 GCS（或本地）的 `news_raw/{日期}/{股票代码}/` 目录下。
    *   **去重：** ✅ 会根据标题、来源和发布时间（精确到分钟）的哈希值进行去重，避免重复抓取和保存相同新闻。

    **用途:**
    可由 Google Cloud Scheduler 等外部调度器定时调用，以确保所有关注股票的最新新闻数据被及时抓取并存储。
    """
    tickers: List[str] = []
    if body.tickers:
        tickers = [t.strip().upper() for t in body.tickers if t.strip()]
    else:
        tickers = [t.strip().upper() for t in settings.default_tickers.split(",") if t.strip()]
    
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force: bool = bool(body.force)
    max_articles: int = int(body.max_articles or settings.max_articles_per_ticker)
    
    results: List[CrawlResult] = []
    for ticker in tickers:
        result = await _crawl_ticker(
            ticker=ticker,
            date_obj=target_date,
            max_articles=max_articles,
            force=force,
        )
        results.append(result)
    return results


@app.post("/crawl/ticker/{ticker}/incremental", response_model=CrawlResult)
async def crawl_ticker_incremental_api(
    ticker: str = Path(..., description="股票代码，如 AAPL"),
    body: TickerRequest = Body(...),
):
    """针对单支股票进行增量抓取。

    **主要行为:** 抓取指定股票在指定日期的最新新闻。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 RSS 源获取指定股票的最新新闻，并尝试抽取正文。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。新抓取的新闻文章将以 JSON 格式保存到 GCS（或本地）的 `news_raw/{日期}/{股票代码}/` 目录下。
    *   **去重：** ✅ 会根据标题、来源和发布时间（精确到分钟）的哈希值进行去重，避免重复抓取和保存相同新闻。

    **用途:**
    适用于单个股票的实时或按需新闻抓取，可由其他服务触发，确保新闻数据是最新且不重复的。
    """
    ticker_norm = ticker.strip().upper()
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    force = bool(body.force)
    max_articles = int(body.max_articles or settings.max_articles_per_ticker)
    result = await _crawl_ticker(
        ticker=ticker_norm,
        date_obj=target_date,
        max_articles=max_articles,
        force=force,
    )
    return result


@app.post(
    "/crawl/topic/macro/fed-funds-rate",
    response_model=TopicCrawlResult,
    summary="抓取美联储联邦基金利率相关新闻",
)
async def crawl_macro_fed_api(
    body: TopicRequest = Body(
        ..., description="宏观主题抓取请求体，以 JSON 形式提交。"
    ),
):
    """
    **主要行为:** 抓取美联储联邦基金利率相关的宏观新闻。

    **关键行动:**
    *   **获取数据:** ✅ 会。从预配置的 RSS 源抓取最新的 Fed Funds Rate 新闻。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。文章会写入 `topic-news/{日期}/macro/Fed_Funds_Rate/`。
    *   **去重：** ✅ 会。沿用现有去重逻辑。

    **用途:**
    适用于宏观主题的增量抓取，供 QA/图卡等服务按需调用。
    """
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None

    try:
        result = await _crawl_topic_dynamic(
            "fed_funds_rate",
            target_date,
            max_articles=max_articles,
            force=force,
        )
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))

    return result


@app.post(
    "/crawl/topic/macro/rate-vs-inflation",
    response_model=TopicCrawlResult,
    summary="抓取 Rate vs Inflation 主题相关新闻",
)
async def crawl_rate_vs_inflation_api(
    body: TopicRequest = Body(..., description="宏观主题抓取请求体，以 JSON 形式提交。"),
):
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None
    try:
        result = await _crawl_topic_dynamic("rate_vs_inflation", target_date, max_articles=max_articles, force=force)
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return result


@app.post(
    "/crawl/topic/macro/rate-heatmap",
    response_model=TopicCrawlResult,
    summary="抓取 Rate Heatmap 主题相关新闻",
)
async def crawl_rate_heatmap_api(
    body: TopicRequest = Body(..., description="宏观主题抓取请求体，以 JSON 形式提交。"),
):
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None
    try:
        result = await _crawl_topic_dynamic("rate_heatmap", target_date, max_articles=max_articles, force=force)
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return result


@app.post(
    "/crawl/topic/macro/macro-dashboard",
    response_model=TopicCrawlResult,
    summary="抓取 Macro Dashboard 主题相关新闻",
)
async def crawl_macro_dashboard_api(
    body: TopicRequest = Body(..., description="宏观主题抓取请求体，以 JSON 形式提交。"),
):
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None
    try:
        result = await _crawl_topic_dynamic("macro_dashboard", target_date, max_articles=max_articles, force=force)
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return result


@app.post(
    "/crawl/topic/dynamic/{topic_key}",
    response_model=TopicCrawlResult,
    summary="按动态配置抓取指定 topic_key 的新闻",
)
async def crawl_dynamic_topic_api(
    topic_key: str = Path(..., description="动态 topic 键，例如 rate_vs_inflation"),
    body: TopicRequest = Body(..., description="抓取请求体"),
):
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None

    try:
        result = await _crawl_topic_dynamic(topic_key, target_date, max_articles=max_articles, force=force)
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))

    return result


@app.post(
    "/crawl/person/{person_key}",
    response_model=PersonCrawlResult,
    summary="抓取人物（celebrity/expert）的相关新闻或社交动态",
)
async def crawl_person_api(
    person_key: str = Path(..., description="人物标识，例如 Elon_Musk"),
    body: PersonRequest = Body(..., description="人物抓取请求体"),
):
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    force = bool(body.force)
    max_articles = int(body.max_articles) if body.max_articles is not None else None

    try:
        result = await _crawl_person_dynamic(
            person_key,
            target_date,
            max_articles=max_articles,
            force=force,
        )
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))

    return result


@app.post("/topic-config/refresh")
async def refresh_topic_config_api():
    try:
        refresh_topic_configs()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}


@app.post("/persons-config/refresh")
async def refresh_persons_config_api():
    try:
        refresh_person_configs()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}

# --- Raw Data Paths API ---

@app.get("/gcs/paths")
async def list_gcs_paths_api(
    date: Optional[str] = Query(None, description="查询日期，格式 YYYY-MM-DD，默认今天"),
    ticker: Optional[str] = Query(
        None,
        description="股票代码或主题路径。股票示例: AAPL；主题示例: macro/Fed_Funds_Rate",
    ),
    news_type: str = Query(
        "stock",
        description="数据类型，stock 表示股票新闻，topic 表示主题新闻。",
    ),
):
    """
    **主要行为:** 查询存储中指定日期（以及可选股票代码）下的原始新闻文章路径列表。

    **关键行动:**
    *   **获取数据:** ✅ 会。根据日期和股票代码从 GCS（或本地）列出所有原始新闻文章的存储路径。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ✅ 会。（返回一个字典，包含前缀和对象相对路径列表）

    **用途:**
    适用于调试、审计或需要获取原始新闻文件存储位置的场景，不包含 AI Context 文件。
    """
    try:
        target_date = _parse_date(date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    date_str = _get_date_dir(target_date)
    if news_type not in {"stock", "topic"}:
        raise HTTPException(status_code=400, detail="news_type must be 'stock' or 'topic'")

    if ticker:
        ticker_norm = ticker.strip().upper() if news_type == "stock" else ticker.strip()
    else:
        ticker_norm = None

    base_prefix = (
        settings.gcs_base_prefix
        if news_type == "stock"
        else settings.gcs_topic_news_prefix
    )
    try:
        if settings.storage_backend == "gcs":
            return _list_gcs_objects(date_str, ticker_norm, base_prefix=base_prefix)
        else:
            return _list_local_objects(date_str, ticker_norm, base_prefix=base_prefix)
    except Exception as e:
        logger.error(f"列出对象失败: {e}")
        raise HTTPException(status_code=500, detail="Failed to list objects")

# --- Download API ---

@app.get("/download/daily/{date_str}")
async def download_daily_raw_news_api(
    date_str: str = Path(..., description="要下载的日期，格式 YYYY-MM-DD")
):
    """
    **主要行为:** 打包并下载指定日期下所有原始新闻文件 (.json)。

    **关键行动:**
    *   **获取数据:** ✅ 会。遍历指定日期目录下的所有股票子目录，下载所有原始新闻 JSON 文件。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 ZIP:** ✅ 会。将所有文件打包成一个 ZIP 压缩包，并可选地保存到 GCS 的对应日期目录下（如果 `STORAGE_BACKEND` 为 `gcs`）。
    *   **提供下载:** ✅ 会。以 `StreamingResponse` 形式提供 ZIP 文件下载给客户端。

    **用途:**
    适用于数据备份、离线分析或将原始新闻数据传输到其他系统进行批量处理的场景。
    """
    try:
        target_date_obj = _parse_date(date_str)
        date_dir_path = _get_date_dir(target_date_obj)
        download_filename = f"raw-news-{date_dir_path}.zip"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            if settings.storage_backend == "gcs":
                if gcs_bucket is None: raise RuntimeError("GCS client not initialized for download.")
                gcs_base_prefix_with_date = f"{settings.gcs_base_prefix}/{date_dir_path}/"
                # NOTE: Bucket.list_blobs() expects only optional keyword arguments such as
                # prefix, delimiter, etc. Passing the bucket name as a positional argument
                # causes it to be misinterpreted as the `client` parameter. This will eventually
                # propagate a string into the underlying page iterator's `max_results` field,
                # resulting in a `TypeError: unsupported operand type(s) for -: 'str' and 'int'`.
                # See the official docs for the correct usage:
                # https://googleapis.dev/python/storage/latest/buckets.html#google.cloud.storage.bucket.Bucket.list_blobs
                # Therefore, call `list_blobs()` with only the prefix keyword argument.
                blobs = gcs_bucket.list_blobs(prefix=gcs_base_prefix_with_date)

                files_found = False
                for blob in blobs:
                    if blob.name.endswith(".manifest.json") or blob.name.endswith("/") or blob.name.startswith(settings.gcs_ai_context_prefix):
                        continue
                    if blob.name.endswith(".json"):
                        files_found = True
                        try:
                            file_content = blob.download_as_bytes()
                            arcname = os.path.relpath(blob.name, gcs_base_prefix_with_date)
                            zip_file.writestr(arcname, file_content)
                            logger.debug(f"Added GCS blob to zip: {blob.name} as {arcname}")
                        except Exception as e:
                            logger.warning(f"无法将 GCS Blob 添加到 ZIP: {blob.name} | {e}")
                
                if not files_found:
                    logger.warning(f"在 GCS 中未找到日期 {date_dir_path} 的原始新闻文件。")

            else: # local storage
                local_full_path = os.path.join(settings.local_storage_root, settings.gcs_base_prefix, date_dir_path)

                if not os.path.isdir(local_full_path):
                    logger.warning(f"本地目录未找到或为空: {local_full_path}")

                files_found = False
                for root, _, files in os.walk(local_full_path):
                    for filename in files:
                        if filename.endswith(".json") and not filename.startswith(".manifest"):
                            files_found = True
                            full_file_path = os.path.join(root, filename)
                            try:
                                with open(full_file_path, "rb") as f:
                                    file_content = f.read()
                                arcname = os.path.relpath(full_file_path, local_full_path)
                                zip_file.writestr(arcname, file_content)
                                logger.debug(f"Added local file to zip: {full_file_path} as {arcname}")
                            except Exception as e:
                                logger.warning(f"无法将本地文件添加到 ZIP: {full_file_path} | {e}")
                
                if not files_found:
                    logger.warning(f"在本地存储中未找到日期 {date_dir_path} 的原始新闻文件。")

        zip_buffer.seek(0)

        if settings.storage_backend == "gcs":
            if gcs_bucket is None: raise RuntimeError("GCS client not initialized for zip upload.")
            gcs_zip_object_name = f"{settings.gcs_base_prefix}/{date_dir_path}/{download_filename}"
            zip_blob = gcs_bucket.blob(gcs_zip_object_name)
            try:
                zip_blob.upload_from_file(zip_buffer, content_type="application/zip")
                logger.info(f"ZIP 文件已成功保存到 GCS: gs://{settings.gcs_bucket_name}/{gcs_zip_object_name}")
                zip_buffer.seek(0)
            except Exception as e:
                logger.error(f"保存 ZIP 文件到 GCS 失败: {gcs_zip_object_name} | {e}", exc_info=True)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={download_filename}"}
        )

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"处理每日原始新闻文件下载/打包失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理每日原始新闻文件下载/打包失败: {e}")
    
@app.get("/download/ai_context/daily/{date_str}")
async def download_ai_context_daily_api(
    date_str: str = Path(..., description="要下载的日期，格式 YYYY-MM-DD")
):
    """
    **主要行为:** 打包并下载指定日期下所有 AI Context 文本文件（.txt）。

    **关键行动:**
    * **获取数据:** ✅ 会。遍历 AI Context 目录（`settings.gcs_ai_context_prefix`）中该日期的所有股票子目录，只收集 `.txt` 文件。
    * **重新生成 AI Context:** ❌ 不会。仅打包已有结果，不触发抓取、清洗或重算。
    * **保存 ZIP:** ✅ 会。将所有 `.txt` 文件打包为一个 ZIP；（可选）若实现中启用保存逻辑且 `STORAGE_BACKEND` 为 `gcs`，可将 ZIP 同步保存到对应日期目录。
    * **提供下载:** ✅ 会。以 `StreamingResponse` 形式返回 ZIP 供客户端下载。

    **用途:**
    适用于对某一日期的 AI Context 结果进行归档备份、线下分析，或向其他系统批量传输既有 AI Context 文本的场景。

    - GCS：遍历 gs://{bucket}/{gcs_ai_context_prefix}/{YYYY/...}/{YYYY-MM-DD}/ 下所有 .txt
    - Local：遍历 {LOCAL_STORAGE_ROOT}/{gcs_ai_context_prefix}/{YYYY/...}/{YYYY-MM-DD}/ 下所有 .txt
    """
    try:
        # 1) 解析日期 -> 构造日期子目录与下载文件名
        target_date_obj = _parse_date(date_str)
        date_dir_path = _get_date_dir(target_date_obj)  # 复用你项目的日期目录规则
        download_filename = f"ai-context-{date_dir_path}.zip"

        # 2) 内存 ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            if settings.storage_backend == "gcs":
                if gcs_bucket is None:
                    raise RuntimeError("GCS client not initialized for download.")
                prefix = f"{settings.gcs_ai_context_prefix}/{date_dir_path}/"

                # 正确用法：Bucket.list_blobs(prefix=...)（不要传 bucket 名当位置参数）
                blobs = gcs_bucket.list_blobs(prefix=prefix)

                files_found = False
                for blob in blobs:
                    # 跳过“目录”对象与非 .txt
                    if blob.name.endswith("/") or not blob.name.endswith(".txt"):
                        continue
                    try:
                        data = blob.download_as_bytes()
                        arcname = os.path.relpath(blob.name, prefix)
                        zip_file.writestr(arcname, data)
                        files_found = True
                    except Exception as e:
                        logger.warning(f"无法将 GCS Blob 写入 ZIP: {blob.name} | {e}")

                if not files_found:
                    logger.warning(f"在 GCS 中未找到日期 {date_dir_path} 的 AI Context .txt 文件。")

            else:
                # Local 模式
                local_root = os.path.join(
                    settings.local_storage_root,
                    settings.gcs_ai_context_prefix,
                    date_dir_path,
                )

                if not os.path.isdir(local_root):
                    logger.warning(f"本地 AI Context 目录未找到或为空: {local_root}")

                files_found = False
                for root, _, files in os.walk(local_root):
                    for fn in files:
                        if not fn.endswith(".txt"):
                            continue
                        full_path = os.path.join(root, fn)
                        try:
                            with open(full_path, "rb") as f:
                                data = f.read()
                            arcname = os.path.relpath(full_path, local_root)
                            zip_file.writestr(arcname, data)
                            files_found = True
                        except Exception as e:
                            logger.warning(f"无法将本地文件写入 ZIP: {full_path} | {e}")

                if not files_found:
                    logger.warning(f"在本地存储中未找到日期 {date_dir_path} 的 AI Context .txt 文件。")

        # 3) 回到缓冲区起始位置
        zip_buffer.seek(0)

        # 4) 若使用 GCS 后端，把 ZIP 同步保存到 GCS（与原 /download/daily 一致的行为）
        if settings.storage_backend == "gcs":
            gcs_zip_object_name = f"{settings.gcs_ai_context_prefix}/{date_dir_path}/{download_filename}"
            zip_blob = gcs_bucket.blob(gcs_zip_object_name)
            try:
                # 注意：上传会消耗 buffer 的当前位置，因此上传后要 seek(0) 再用于响应下载
                zip_blob.upload_from_file(zip_buffer, content_type="application/zip")
                logger.info(
                    f"AI Context ZIP 已保存至 GCS: gs://{settings.gcs_bucket_name}/{gcs_zip_object_name}"
                )
                zip_buffer.seek(0)
            except Exception as e:
                logger.error(
                    f"保存 AI Context ZIP 到 GCS 失败: {gcs_zip_object_name} | {e}",
                    exc_info=True
                )
                # 即使上传失败，仍继续返回下载

        # 5) 本地下载
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={download_filename}"},
        )

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"处理 AI Context 下载/打包失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理 AI Context 下载/打包失败: {e}")


# --- Diagnostics API Routes ---


@app.post(
    "/diagnostics/rss",
    response_model=RSSDiagnosticsResponse,
    summary="诊断 topic_configs 中配置的 RSS 源可用性",
)
async def diagnose_rss_sources_api(
    body: RSSDiagnosticsRequest = Body(
        ..., description="RSS 诊断请求体，以 JSON 形式提交。"
    ),
):
    """
    **主要行为:** 对动态配置中的 RSS 源执行可用性与正文抽取诊断。

    **关键行动:**
    *   **逐源抓取:** ✅ 会。为每个 RSS 源解析若干最新条目，并尝试抽取正文。
    *   **记录过滤原因:** ✅ 会。输出命中过滤、正文缺失等原因，便于调整配置。
    *   **持久化:** ❌ 不会。只返回实时诊断数据，不写入存储。

    **请求体字段:**
    *   `topics` (可选 `List[str]`): 指定要诊断的 topic key/别名，省略则遍历全部配置。
    *   `max_entries_per_source` (可选 `int`): 每个 RSS 源最多检查的文章数量，默认 3，范围 1–10。
    *   `enable_debug_log` (可选 `bool`): 是否在本次诊断期间临时开启 DEBUG 日志，方便排查。
    *   `refresh_configs` (可选 `bool`): 在诊断前是否调用 `refresh_topic_configs()` 重新加载配置。
    """
    if body.refresh_configs:
        refresh_topic_configs()

    diagnostics = await diagnose_rss_sources(
        topic_keys=body.topics,
        max_entries_per_source=int(body.max_entries_per_source or 3),
        enable_debug_log=bool(body.enable_debug_log),
    )

    return RSSDiagnosticsResponse(
        total_sources=len(diagnostics),
        diagnostics=diagnostics,
    )


@app.post(
    "/diagnostics/persons/rss",
    response_model=RSSDiagnosticsResponse,
    summary="诊断 persons_config 中配置的 RSS 源可用性",
)
async def diagnose_person_rss_sources_api(
    body: RSSDiagnosticsRequest = Body(
        ..., description="人物 RSS 诊断请求体，以 JSON 形式提交。"
    ),
):
    if body.refresh_configs:
        refresh_person_configs()

    diagnostics = await diagnose_rss_sources(
        topic_keys=body.topics,
        max_entries_per_source=int(body.max_entries_per_source or 3),
        enable_debug_log=bool(body.enable_debug_log),
        get_config=get_person_config,
        get_all_configs=get_all_person_configs,
        registry_label="person",
    )

    return RSSDiagnosticsResponse(
        total_sources=len(diagnostics),
        diagnostics=diagnostics,
    )


# --- AI Context API Routes ---

@app.post("/generate-ai-context/daily")
async def generate_daily_ai_context_api(
    body: GenerateDailyAIContextRequest = Body(..., description="生成每日AI上下文的请求体。")
) -> Dict[str, Any]:
    """
    **主要行为:** 为所有默认关注的股票在指定日期（或今天）生成每日新闻 AI Context。

    **关键行动:**
    *   **获取数据:** ✅ 会。加载 GCS 中指定日期下已抓取的原始新闻文章。
    *   **生成 AI Context:** ✅ 会。根据原始新闻文章（优先带正文、内容质量高的）生成整合的 AI Context 文本。
    *   **保存 JSON:** ✅ 会。生成的 AI Context 文本将保存到 GCS 的 `ai_context/{日期}/{股票代码}_{时间戳}.txt` 目录下。
    *   **更新索引:** ✅ 会。在生成并保存 AI Context 后，同时更新 `ai_context/daily_index/{日期}.json` 索引文件，其中包含当天生成的所有 AI Context 的 GCS 路径。

    **用途:**
    可由 Google Cloud Scheduler 等外部调度器定时调用，以确保所有默认关注股票的最新新闻 AI Context 及时生成并可供 QA 引擎使用。
    """
        
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    tickers_to_process: List[str] = [t.strip().upper() for t in settings.default_tickers.split(",") if t.strip()]

    if not tickers_to_process:
        raise HTTPException(status_code=400, detail="Default tickers are empty in settings.")

    results = {}
    for ticker in tickers_to_process:
        logger.info(f"Generating AI context for {ticker} on {target_date}...")
        try:
            articles = await _load_articles_for_ticker_date(target_date, ticker)
            # Determine which pipeline steps to output from environment variable
            import os
            steps_env = os.getenv("AI_CONTEXT_OUTPUT_STEPS", "2")
            steps_to_output: list[int] = []
            for s in steps_env.split(","):
                s = s.strip()
                if s.isdigit():
                    try:
                        steps_to_output.append(int(s))
                    except Exception:
                        continue
            if not steps_to_output:
                steps_to_output = [2]

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
                results[ticker] = {
                    "status": "skipped",
                    "message": "No high-quality news to generate context.",
                    "articles_processed": len(articles),
                }
                continue
            # Identify final step
            final_step = max(pipeline_outputs.keys())
            saved_paths: Dict[int, str] = {}
            for step_num, context_content in pipeline_outputs.items():
                # Skip diagnostic messages
                if context_content.startswith("No articles") or context_content.startswith("No high-quality"):
                    continue
                update_index = bool(step_num == final_step)
                path = await _save_ai_context_step(
                    target_date,
                    ticker,
                    context_content,
                    step_num,
                    update_index=update_index,
                )
                saved_paths[step_num] = path
            if not saved_paths:
                results[ticker] = {
                    "status": "skipped",
                    "message": "No high-quality news to generate context.",
                    "articles_processed": len(articles),
                }
            else:
                # final step path
                final_path = saved_paths.get(final_step)
                results[ticker] = {
                    "status": "success",
                    "ai_context_path": final_path,
                    "articles_processed": len(articles),
                    "saved_steps": saved_paths,
                }
                
        except Exception as e:
            logger.error(f"Failed to generate AI context for {ticker} on {target_date}: {e}", exc_info=True)
            results[ticker] = {"status": "failed", "error": str(e)}

    return {"date": _get_date_dir(target_date), "results": results}


@app.post("/generate-ai-context/ticker/{ticker}")
async def generate_single_ticker_ai_context_api(
    ticker: str = Path(..., description="股票代码，如 AAPL"),
    body: GenerateTickerAIContextRequest = Body(..., description="生成单个股票AI上下文的请求体。")
) -> Dict[str, Any]:
    """
    **主要行为:** 为指定股票和日期（或今天）生成新闻 AI Context。

    **关键行动:**
    *   **获取数据:** ✅ 会。加载 GCS 中指定日期下已抓取的原始新闻文章。
    *   **生成 AI Context:** ✅ 会。根据原始新闻文章（优先带正文、内容质量高的）生成整合的 AI Context 文本。
    *   **保存 JSON:** ✅ 会。生成的 AI Context 文本将保存到 GCS 的 `ai_context/{日期}/{股票代码}_{时间戳}.txt` 目录下。
    *   **更新索引:** ✅ 会。在生成并保存 AI Context 后，同时更新 `ai_context/daily_index/{日期}.json` 索引文件，其中包含当天生成的所有 AI Context 的 GCS 路径。

    **用途:**
    适用于需要按需为特定股票生成最新新闻 AI Context 的场景，例如在系统检测到重要事件发生时，或用于调试。
    """
    ticker_norm = ticker.strip().upper()
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"Generating AI context for single ticker {ticker_norm} on {target_date}...")
    try:
        articles = await _load_articles_for_ticker_date(target_date, ticker_norm)
        # Determine which pipeline steps to output from environment variable
        import os
        steps_env = os.getenv("AI_CONTEXT_OUTPUT_STEPS", "2")
        steps_to_output: list[int] = []
        for s in steps_env.split(","):
            s = s.strip()
            if s.isdigit():
                try:
                    steps_to_output.append(int(s))
                except Exception:
                    continue
        if not steps_to_output:
            steps_to_output = [2]

        from news_crawler.ai_context import (
            _prepare_ai_context_pipeline,
            _save_ai_context_step,
        )
        pipeline_outputs = await _prepare_ai_context_pipeline(
            articles,
            target_date,
            ticker_norm,
            steps_to_output=steps_to_output,
            max_articles_for_context=None,
        )
        if not pipeline_outputs:
            return {
                "ticker": ticker_norm,
                "date": _get_date_dir(target_date),
                "status": "skipped",
                "message": "No high-quality news to generate context.",
                "articles_processed": len(articles),
            }
        final_step = max(pipeline_outputs.keys())
        saved_paths: Dict[int, str] = {}
        for step_num, context_content in pipeline_outputs.items():
            if context_content.startswith("No articles") or context_content.startswith("No high-quality"):
                continue
            update_index = bool(step_num == final_step)
            path = await _save_ai_context_step(
                target_date,
                ticker_norm,
                context_content,
                step_num,
                update_index=update_index,
            )
            saved_paths[step_num] = path
        if not saved_paths:
            return {
                "ticker": ticker_norm,
                "date": _get_date_dir(target_date),
                "status": "skipped",
                "message": "No high-quality news to generate context.",
                "articles_processed": len(articles),
            }
        final_path = saved_paths.get(final_step)
        return {
            "ticker": ticker_norm,
            "date": _get_date_dir(target_date),
            "status": "success",
            "ai_context_path": final_path,
            "articles_processed": len(articles),
            "saved_steps": saved_paths,
        }

    except Exception as e:
        logger.error(f"Failed to generate AI context for {ticker_norm} on {target_date}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate AI context: {e}")

# --- Batch Processing API Routes ---

@app.post("/batch_process_all", summary="【Cloud Scheduler调用】全量抓取并生成所有默认股票的AI Context")
async def batch_process_all_api() -> Dict[str, Any]:
    """
    **主要行为:** 执行所有默认关注股票的完整每日批量处理，包括：
    1.  抓取当天最新的新闻（增量，非强制）。
    2.  根据当天所有已抓取的新闻，生成并保存每支股票的 AI Context。
    3.  更新当日 AI Context 清单。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 RSS 源增量抓取默认股票列表的最新新闻。
    *   **生成 AI Context:** ✅ 会。
    *   **保存 JSON:** ✅ 会。原始新闻数据和 AI Context 文本都会保存到 GCS。
    *   **提供路径:** ✅ 会。（返回每支股票 AI Context 生成结果及存储路径）

    **用途:**
    作为核心每日任务，由 Cloud Scheduler 等外部调度器定时调用，确保所有新闻数据和 AI Context 保持最新。此接口无需任何输入参数。
    """
    # 始终使用当前日期
    target_date = _parse_date(None)
    date_str = _get_date_dir(target_date)
    logger.info(f"开始为日期 {date_str} 执行所有默认股票的批量处理...")
    
    tickers_to_process: List[str] = [t.strip().upper() for t in settings.default_tickers.split(",") if t.strip()]

    if not tickers_to_process:
        raise HTTPException(status_code=400, detail="Default tickers are empty in settings.")

    results: Dict[str, Dict] = {}
    for ticker in tickers_to_process:
        results[ticker] = await _process_ticker_for_batch(ticker, target_date)
    
    logger.info(f"日期 {date_str} 的批量处理完成。")
    return {"message": "All default tickers have been processed.", "date": date_str, "results": results}


@app.post(
    "/batch/process_topic/macro/fed-funds-rate",
    summary="【Batch Job调用】处理美联储利率新闻及AI Context",
)
async def batch_process_macro_fed_api(
    body: TopicRequest = Body(
        ..., description="包含处理日期的请求体，默认为今天。"
    ),
) -> Dict[str, Any]:
    """
    **主要行为:** 一次性完成 Fed Funds Rate 新闻抓取与 AI Context 生成。

    **关键行动:**
    *   **获取数据:** ✅ 会。调用宏观抓取流程写入 `topic-news`。
    *   **生成 AI Context:** ✅ 会。运行步骤 1/2 管线并写入 `ai-context/{date}`。
    *   **更新索引:** ✅ 会。最终步路径会追加到 `daily_index`。

    **用途:**
    适合调度任务每日生成宏观主题的 AI Context，供下游直接消费。
    """
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(
        f"开始为宏观主题 Fed Funds Rate 在日期 {target_date} 执行批量处理..."
    )

    result = await _process_topic_for_batch("fed_funds_rate", target_date)

    logger.info(
        f"宏观主题 Fed Funds Rate 在日期 {target_date} 的批量处理完成。"
    )
    return result

@app.post(
    "/batch/process_topic/dynamic/{topic_key}",
    summary="【Batch Job调用】按动态配置处理任意 topic 的新闻及AI Context",
)
async def batch_process_dynamic_topic_api(
    topic_key: str = Path(..., description="动态 topic 键，例如 rate_vs_inflation"),
    body: TopicRequest = Body(..., description="包含处理日期的请求体，默认为今天。"),
) -> Dict[str, Any]:
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"开始为动态主题 {topic_key} 在日期 {target_date} 执行批量处理...")
    result = await _process_topic_for_batch(topic_key, target_date)
    logger.info(f"动态主题 {topic_key} 在日期 {target_date} 的批量处理完成。 result={result.get('status')}")
    return result


@app.post(
    "/batch/process_person/dynamic/{person_key}",
    summary="【Batch Job调用】按人物配置处理新闻并生成AI Context",
)
async def batch_process_dynamic_person_api(
    person_key: str = Path(..., description="人物键，例如 Elon_Musk"),
    body: PersonRequest = Body(..., description="包含处理日期的请求体，默认为今天。"),
) -> Dict[str, Any]:
    """
    **主要行为:** 抓取指定人物的新闻/社交动态，并运行 AI Context 管线。

    **关键行动:**
    *   **获取数据:** ✅ 会。依据人物配置抓取 RSS 源，并写入 `person-news`。
    *   **生成 AI Context:** ✅ 会。运行步骤 1/2 管线写入 `ai-context/{date}`。
    *   **更新索引:** ✅ 会。最终步骤加入每日索引，便于下游消费。
    """

    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("开始为人物 %s 在日期 %s 执行批量处理...", person_key, target_date)
    result = await _process_person_for_batch(person_key, target_date)
    logger.info(
        "人物 %s 在日期 %s 的批量处理完成。 result=%s",
        person_key,
        target_date,
        result.get("status"),
    )
    return result


@app.post("/batch/process_ticker/{ticker}", summary="【Batch Job调用】处理单个股票的新闻抓取和AI Context生成")
async def batch_process_single_ticker_api(
    ticker: str = Path(..., description="要处理的股票代码，如 AAPL"),
    body: GenerateTickerAIContextRequest = Body(..., description="包含处理日期的请求体，默认为今天。")
) -> Dict[str, Any]:
    """
    **主要行为:** 为单个股票执行完整的新闻处理流程，包括抓取和AI Context生成。

    **关键行动:**
    1.  **增量抓取:** ✅ 会。为指定股票和日期抓取最新新闻。
    2.  **生成AI Context:** ✅ 会。使用当天所有新闻为该股票生成AI Context。
    3.  **保存与更新索引:** ✅ 会。保存所有产物（原始新闻、AI Context）并更新每日索引。

    **用途:**
    专为外部批量处理作业（如 Cloud Run Job）设计。作业可以遍历一个股票列表，并为每个股票调用此接口，从而实现对任意股票列表在任意日期的处理，取代了顺序调用两个独立API的繁琐流程。
    """
    ticker_norm = ticker.strip().upper()
    try:
        target_date = _parse_date(body.date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"开始为股票 {ticker_norm} 在日期 {target_date} 执行批量处理...")

    # 核心逻辑已封装在 _process_ticker_for_batch 中
    result = await _process_ticker_for_batch(ticker_norm, target_date)

    logger.info(f"股票 {ticker_norm} 在日期 {target_date} 的批量处理完成。")
    return result

# --- API for QA Engine to fetch AI Context Paths ---

@app.get("/ai_context/daily_index", summary="获取指定日期的所有股票新闻AI Context清单")
async def get_daily_ai_context_index_api(
    date: Optional[str] = Query(None, description="日期格式: YYYY-MM-DD, 默认为今天")
) -> Dict[str, Any]:
    """
    **主要行为:** 获取指定日期所有已生成新闻 AI Context 文件的清单。
    清单中包含当天为不同股票生成的所有 AI Context 文件路径，并按最新生成的时间戳排序。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 GCS 加载当日的 AI Context 索引文件 (`ai_context/daily_index/{日期}.json`)。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ✅ 会。（返回一个字典，包含日期和当天所有股票的 AI Context 文件在 GCS 上的路径列表）

    **用途:**
    供 QA 引擎或其他下游服务批量获取当日所有更新过的新闻 AI Context 文件路径，以便进行后续处理（如批量喂给 LLM）。
    """
    target_date = _parse_date(date)
    index_list = _load_daily_ai_context_index(target_date)
    return {"date": _get_date_dir(target_date), "items": index_list}


@app.get("/ai_context/{ticker}/by_date/{date_str}", summary="获取某支股票特定日期最新的新闻AI Context路径")
async def get_latest_ai_context_path_by_specific_date_api(
    ticker: str = Path(..., description="股票代码，如 AAPL"),
    date_str: str = Path(..., description="目标日期，格式 YYYY-MM-DD")
) -> Dict[str, Any]:
    """
    **主要行为:** 获取指定股票在特定日期生成的**最新**新闻 AI Context 文件在 GCS 上的路径。
    此接口会返回当日为该股票生成的最新的 AI Context 路径，需要提供明确的日期。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 GCS 加载当日的 AI Context 索引文件，并筛选出指定股票的路径。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **提供路径:** ✅ 会。（返回一个字典，包含股票代码、日期和最新 AI Context 文件的 GCS 路径）

    **用途:**
    供 QA 或需要精确查找特定股票在特定日期的**最新**新闻 AI Context 文件的场景使用。调用方需要自行去 GCS 读取该路径下的内容。
    """
    try:
        target_date = _parse_date(date_str)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    return await _get_latest_ai_context_path_common_logic(ticker, target_date)


@app.get("/ai_context/{ticker}/by_date", summary="获取某支股票今天最新的新闻AI Context路径")
async def get_latest_ai_context_path_by_today_api(
    ticker: str = Path(..., description="股票代码，如 AAPL")
) -> Dict[str, Any]:
    """
    **主要行为:** 获取指定股票在**今天**生成的**最新**新闻 AI Context 文件在 GCS 上的路径。
    此接口会返回当日为该股票生成的最新的 AI Context 路径，无需提供日期，默认今天。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 GCS 加载当日（今天）的 AI Context 索引文件，并筛选出指定股票的路径。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **提供路径:** ✅ 会。（返回一个字典，包含股票代码、日期和最新 AI Context 文件的 GCS 路径）

    **用途:**
    供 QA 或需要获取特定股票在**今天**的最新新闻 AI Context 文件的场景使用。调用方需要自行去 GCS 读取该路径下的内容。
    """
    target_date = _parse_date(None)
    return await _get_latest_ai_context_path_common_logic(ticker, target_date)

# --- Health Check ---

@app.get("/health")
async def health_check():
    """简单健康检查端点。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting {settings.app_name} with log level {settings.log_level}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
