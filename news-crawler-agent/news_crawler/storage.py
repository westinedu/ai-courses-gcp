# news_crawler/storage.py
import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from slugify import slugify # 确保导入 slugify
from fastapi import HTTPException

from news_crawler.utils import _get_date_dir,_parse_date

# 导入 settings
from settings import settings # *** 注意：这里导入 settings 的路径已修改 ***

logger = logging.getLogger("news_crawler_agent")

# --- GCS 客户端初始化 (移到顶部) ---
gcs_client = None
gcs_bucket = None

if settings.storage_backend == "gcs":
    if not settings.gcs_bucket_name: # 明确检查gcs_bucket_name是否已配置
        logger.error("GCS_BUCKET_NAME is not set, but STORAGE_BACKEND is 'gcs'.")
        raise ValueError("GCS_BUCKET_NAME must be configured when STORAGE_BACKEND is 'gcs'.")
    try:
        from google.cloud import storage  # type: ignore
        gcs_client = storage.Client()
        gcs_bucket = gcs_client.bucket(settings.gcs_bucket_name) 
        logger.info("GCS client initialized successfully.")
    except Exception as e:
        logger.error(f"初始化 GCS 客户端失败: {e}")
        raise # 抛出异常，阻止服务在GCS配置错误时启动


# --- Local Storage Operations ---

def _save_local_article(
    article_json: Dict[str, Any],
    date_str: str,
    ticker: str,
    url_hash: str,
    *,
    base_prefix: Optional[str] = None,
) -> str:
    """将文章 JSON 保存到本地文件系统。

    返回保存的相对文件路径（相对于 base_prefix/date/ticker）。
    """
    prefix = base_prefix or settings.gcs_base_prefix
    base_dir = os.path.join(settings.local_storage_root, prefix)
    date_dir = os.path.join(base_dir, date_str)
    ticker_dir = os.path.join(date_dir, ticker)
    os.makedirs(ticker_dir, exist_ok=True)
    
    now_ts = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d%H%M%S")
    source_slug = slugify(article_json.get("source", "")) or "source"
    title_slug = slugify(article_json.get("title", ""))[:60] or "article"
    filename = f"{now_ts}_{source_slug}_{title_slug}_{url_hash[:8]}.json"
    file_path = os.path.join(ticker_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(article_json, f, ensure_ascii=False, indent=2)
    
    rel_path = os.path.join(date_str, ticker, filename)
    return rel_path


def _load_local_manifest(date_str: str, *, base_prefix: Optional[str] = None) -> Dict[str, Any]:
    """加载或初始化本地 manifest。

    Manifest 文件记录当前日期已保存的 URL 哈希列表，避免重复抓取。
    返回字典形式：{"hashes": [...], "files": [...]}。
    """
    prefix = base_prefix or settings.gcs_base_prefix
    base_dir = os.path.join(settings.local_storage_root, prefix)
    date_dir = os.path.join(base_dir, date_str)
    os.makedirs(date_dir, exist_ok=True)
    manifest_path = os.path.join(date_dir, ".manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
                manifest.setdefault("hashes", [])
                manifest.setdefault("files", [])
                return manifest
        except Exception:
            logger.warning(f"无法解析 manifest 文件: {manifest_path}，将重新创建。")
    return {"hashes": [], "files": []}


def _save_local_manifest(
    date_str: str,
    manifest: Dict[str, Any],
    *,
    base_prefix: Optional[str] = None,
) -> None:
    """保存 manifest 到本地文件系统。"""
    prefix = base_prefix or settings.gcs_base_prefix
    base_dir = os.path.join(settings.local_storage_root, prefix)
    date_dir = os.path.join(base_dir, date_str)
    os.makedirs(date_dir, exist_ok=True)
    manifest_path = os.path.join(date_dir, ".manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _list_local_objects(
    date_str: str,
    ticker: Optional[str] = None,
    *,
    base_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """列出本地目录下的对象路径。

    返回字典，包括 ``prefix`` 和 ``objects`` 列表。其中 ``prefix`` 是
    ``settings.gcs_base_prefix/date_str/[ticker/]``，``objects`` 列出
    指定前缀下的文件名（不含前缀部分）。
    """
    prefix = base_prefix or settings.gcs_base_prefix
    base_dir = os.path.join(settings.local_storage_root, prefix)
    date_dir = os.path.join(base_dir, date_str)
    if ticker:
        prefix_path = f"{prefix}/{date_str}/{ticker}/"
        target_dir = os.path.join(date_dir, ticker)
    else:
        prefix_path = f"{prefix}/{date_str}/"
        target_dir = date_dir
    objects: List[str] = []
    if os.path.isdir(target_dir):
        for root, _, files in os.walk(target_dir):
            for fname in files:
                if fname.endswith(".json") and not fname.startswith(".manifest"):
                    rel_dir = os.path.relpath(root, date_dir)
                    rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
                    objects.append(rel_path)
    return {"prefix": prefix_path, "objects": sorted(objects)}


# --- GCS Storage Operations ---

async def _save_gcs_article(
    article_json: Dict[str, Any],
    date_str: str,
    ticker: str,
    url_hash: str,
    *,
    base_prefix: Optional[str] = None,
) -> str:
    """将文章 JSON 保存到 Google Cloud Storage。

    返回存储对象的相对路径（即前缀后面的部分）。
    """
    if gcs_client is None or gcs_bucket is None:
        raise RuntimeError("GCS client not initialized. Cannot save to GCS.")

    now_ts = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d%H%M%S")
    source_slug = slugify(article_json.get("source", "")) or "source"
    title_slug = slugify(article_json.get("title", ""))[:60] or "article"
    filename = f"{now_ts}_{source_slug}_{title_slug}_{url_hash[:8]}.json"
    
    prefix = base_prefix or settings.gcs_base_prefix
    object_name = f"{prefix}/{date_str}/{ticker}/{filename}"
    blob = gcs_bucket.blob(object_name)
    try:
        blob.upload_from_string(
            json.dumps(article_json, ensure_ascii=False, indent=2),
            content_type="application/json",
        )
    except Exception as e:
        logger.error(f"上传到 GCS 失败: {object_name} | {e}")
        raise
    return os.path.join(date_str, ticker, filename)


def _load_gcs_manifest(date_str: str, *, base_prefix: Optional[str] = None) -> Dict[str, Any]:
    """从 GCS 加载或初始化 manifest。"""
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. Cannot load GCS manifest.")
        return {"hashes": [], "files": []}

    prefix = base_prefix or settings.gcs_base_prefix
    object_name = f"{prefix}/{date_str}/.manifest.json"
    blob = gcs_bucket.blob(object_name)
    try:
        if blob.exists():
            data = blob.download_as_text(encoding="utf-8")
            manifest = json.loads(data)
            manifest.setdefault("hashes", [])
            manifest.setdefault("files", [])
            return manifest
    except Exception as e:
        logger.warning(f"读取 GCS manifest 失败: {object_name} | {e}")
    return {"hashes": [], "files": []}


def _save_gcs_manifest(
    date_str: str,
    manifest: Dict[str, Any],
    *,
    base_prefix: Optional[str] = None,
) -> None:
    """保存 manifest 到 GCS。"""
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. Cannot save GCS manifest.")
        return

    prefix = base_prefix or settings.gcs_base_prefix
    object_name = f"{prefix}/{date_str}/.manifest.json"
    blob = gcs_bucket.blob(object_name)
    try:
        blob.upload_from_string(json.dumps(manifest, ensure_ascii=False, indent=2), content_type="application/json")
    except Exception as e:
        logger.error(f"保存 GCS manifest 失败: {object_name} | {e}")
        raise


def _list_gcs_objects(
    date_str: str,
    ticker: Optional[str] = None,
    *,
    base_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """列出 GCS 中指定前缀下的对象列表。"""
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. Cannot list GCS objects.")
        return {"prefix": "", "objects": []}

    prefix_root = base_prefix or settings.gcs_base_prefix
    if ticker:
        prefix = f"{prefix_root}/{date_str}/{ticker}/"
    else:
        prefix = f"{prefix_root}/{date_str}/"
    blobs = gcs_client.list_blobs(settings.gcs_bucket_name, prefix=prefix)
    objects: List[str] = []
    base_len = len(prefix)
    for blob in blobs:
        name = blob.name
        if name.endswith(".manifest.json") or name.endswith("/") or name.startswith(settings.gcs_ai_context_prefix):
            continue
        if name.endswith(".json"):
            objects.append(name[base_len:])
    return {"prefix": prefix, "objects": sorted(objects)}


# --- AI Context Daily Index Helper Functions ---

def _get_gcs_ai_context_index_blob_name(target_date: date) -> str:
    """获取每日 AI Context 索引文件的 GCS Blob 名称。"""
    date_str = _get_date_dir(target_date)
    return f"{settings.gcs_ai_context_prefix}/daily_index/{date_str}.json"

def _load_daily_ai_context_index(target_date: date) -> List[Dict[str, Any]]:
    """从 GCS 加载指定日期的 AI Context 索引。"""
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. AI context index cannot be loaded from GCS.")
        return []
    
    object_name = _get_gcs_ai_context_index_blob_name(target_date)
    blob = gcs_bucket.blob(object_name)
    try:
        if blob.exists():
            data = blob.download_as_text(encoding="utf-8")
            index_list = json.loads(data)
            if not isinstance(index_list, list):
                logger.warning(f"AI Context daily index for {target_date} is not a list. Recreating.")
                return []
            for item in index_list:
                item.setdefault("timestamp", datetime.min.isoformat())
            return index_list
    except Exception as e:
        logger.warning(f"读取 GCS AI Context daily index 失败: {object_name} | {e}")
    return [] # 这里应该返回空列表，因为它期望的是List

def _save_daily_ai_context_index(target_date: date, index_list: List[Dict[str, Any]]) -> None:
    """保存 AI Context 索引到 GCS。"""
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. AI context index will not be saved to GCS.")
        return
    
    object_name = _get_gcs_ai_context_index_blob_name(target_date)
    blob = gcs_bucket.blob(object_name)
    try:
        json_data = json.dumps(index_list, ensure_ascii=False, indent=2)
        blob.upload_from_string(json_data, content_type="application/json")
        logger.info(f"AI Context daily index for {target_date} saved to GCS: gs://{settings.gcs_bucket_name}/{object_name}")
    except Exception as e:
        logger.error(f"保存 GCS AI Context daily index 失败: {object_name} | {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"保存 GCS AI Context daily index 失败: {e}")

async def _append_daily_ai_context_index(
    target_date: date,
    ticker: str,
    gcs_path: str,
    *,
    preserve_case: bool = False,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> None:
    """
    将新的 AI Context 路径追加到当天的 AI Context 索引中。
    会加载现有索引，添加新条目，并按时间戳降序保存。
    """
    if gcs_client is None or gcs_bucket is None:
        logger.warning("GCS client not initialized. AI context index will not be updated.")
        return
    
    index_list = _load_daily_ai_context_index(target_date)
    
    now_utc = datetime.now(ZoneInfo("UTC"))
    
    ticker_key = ticker.strip() if preserve_case else ticker.strip().upper()

    new_entry: Dict[str, Any] = {
        "ticker": ticker_key,
        "path": gcs_path,
        "timestamp": now_utc.isoformat(),
    }

    if extra_fields:
        new_entry.update(extra_fields)
    
    # 移除该股票的旧条目，这样每次只添加一个 ticker 的最新条目，保持每日索引简洁，只记录最新生成的 AI Context
    index_list = [
        item
        for item in index_list
        if not (item.get("ticker") == ticker_key and item.get("path") == gcs_path)
    ]
    index_list.append(new_entry)
        
    index_list.sort(key=lambda x: datetime.fromisoformat(x.get("timestamp", datetime.min.isoformat())).astimezone(ZoneInfo("UTC")), reverse=True)
    
    _save_daily_ai_context_index(target_date, index_list)
