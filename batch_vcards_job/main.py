# main.py
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from google.cloud import storage
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# --- 配置日志 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
)
logger = logging.getLogger(__name__)


# --- 通过环境变量加载配置 ---
class Settings(BaseSettings):
    gcs_bucket_name: str = Field(..., description="GCS 存储桶名称")
    ticker_list_path: str = Field("batch_config/ticker_list.json", description="GCS 中股票列表文件的路径")
    card_types_path: str = Field("batch_config/card_types.json", description="GCS 中图卡类型文件的路径")
    targets_path: str = Field("batch_config/card_targets.json", description="GCS 中图卡目标配置文件的路径")
    # --- 新增配置项 ---
    llm_config_path: str = Field("batch_config/llm_config.json", description="GCS 中 LLM 配置文件的路径")
    # --- 新增配置项 ---
    engine_control_path: str = Field("batch_config/engine_control.json", description="GCS 中数据引擎控制文件的路径")

    financial_engine_url: str = Field(..., description="财报数据引擎的 URL")
    trading_engine_url: str = Field(..., description="交易数据引擎的 URL")
    news_engine_url: str = Field(..., description="新闻爬虫引擎的 URL")
    qa_engine_url: str = Field(..., description="QA 引擎的 URL")
    
    request_timeout: int = Field(300, description="对下游服务的请求超时时间（秒）")

settings = Settings()

# --- GCS 辅助函数 ---
def load_gcs_json(bucket_name: str, blob_path: str) -> Any:
    """从 GCS 加载并解析 JSON 文件 (可以是列表或字典)"""
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        
        logger.info(f"正在从 gs://{bucket_name}/{blob_path} 加载配置...")
        
        if not blob.exists():
            logger.warning(f"配置文件 gs://{bucket_name}/{blob_path} 不存在，将返回 None。")
            return None

        content = blob.download_as_text()
        data = json.loads(content)
        
        item_count = len(data) if isinstance(data, list) else 1
        logger.info(f"成功加载 {item_count} 个项目。")
        return data
    except Exception as e:
        logger.error(f"从 GCS 加载配置文件 {blob_path} 失败: {e}", exc_info=True)
        raise



def _slugify_segment(value: str) -> str:
    """Lowercase slug used for dynamic topic endpoints (retain underscores)."""
    if not value:
        return ""
    lowered = value.strip().lower()
    normalised = lowered.replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_]+", "", normalised)
    return cleaned


def _expand_card_targets(
    raw_targets: Any,
    card_types_enabled: List[str],
    bucket_name: str,
):
    """解析 card_targets.json，返回补充目标和股票卡片配置。"""
    if not isinstance(raw_targets, list):
        return [], {}

    expanded: List[Dict[str, Any]] = []
    equities_card_types: List[str] = []
    equities_run_overrides: Dict[str, Any] = {}

    for entry in raw_targets:
        if not isinstance(entry, dict):
            continue

        logger.info("解析 card_target entry: %s", json.dumps(entry, ensure_ascii=False))

        entry_id = str(entry.get("id") or "").strip()
        raw_card_types = entry.get("card_types")
        if isinstance(raw_card_types, list) and raw_card_types:
            card_types = [ct for ct in raw_card_types if ct in card_types_enabled]
        else:
            card_types = [ct for ct in card_types_enabled]

        run_overrides = (
            entry.get("run_engines") if isinstance(entry.get("run_engines"), dict) else {}
        )
        extra_params = (
            entry.get("extra_params") if isinstance(entry.get("extra_params"), dict) else {}
        )
        category = entry.get("category") or entry.get("type") or ""
        category_norm = category.strip().lower()
        target_type = (entry.get("target_type") or "").strip().lower()
        if not target_type:
            if category_norm in {"celebrity", "person", "people"}:
                target_type = "person"
            elif category_norm == "equity":
                target_type = "equity"
            else:
                target_type = "topic"

        if entry_id.lower() == "equities_default":
            equities_card_types = list(card_types)
            equities_run_overrides = run_overrides.copy() if run_overrides else {}
            logger.info("  -> 保存 equities_default 设置: card_types=%s, run_overrides=%s",
                        equities_card_types, equities_run_overrides)
            continue

        if not entry_id:
            logger.warning("  -> 跳过无效 card_target (缺少 id)。")
            continue

        expanded.append(
            {
                "ticker": entry_id,
                "card_types": list(card_types),
                "run_overrides": run_overrides.copy() if run_overrides else {},
                "extra_params": extra_params.copy() if extra_params else {},
                "category": category,
                "date": entry.get("date"),
                "target_id": entry_id,
                "is_topic": target_type == "topic",
                "target_type": target_type,
            }
        )

    equities_settings = {
        "card_types": equities_card_types,
        "run_overrides": equities_run_overrides,
    }
    return expanded, equities_settings


def _resolve_engine_flags(global_flags: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, bool]:
    resolved = {
        "financials": bool(global_flags.get("run_financials_engine", False)),
        "trading": bool(global_flags.get("run_trading_engine", False)),
        "news": bool(global_flags.get("run_news_engine", False)),
    }
    for key, value in (overrides or {}).items():
        if key in ("financials", "trading", "news"):
            resolved[key] = bool(value)
        elif key == "run_financials_engine":
            resolved["financials"] = bool(value)
        elif key == "run_trading_engine":
            resolved["trading"] = bool(value)
        elif key == "run_news_engine":
            resolved["news"] = bool(value)
    return resolved

# --- 认证辅助函数 ---
async def get_auth_token(client: httpx.AsyncClient, audience_url: str) -> str:
    """
    为 Cloud Run 服务间调用获取 OIDC 身份令牌。
    """
    # 仅在实际云环境中（非本地模拟）执行
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        logger.warning("非 GCP 环境，跳过认证令牌获取。")
        return ""

    try:
        metadata_server_url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience_url}"
        headers = {"Metadata-Flavor": "Google"}
        response = await client.get(metadata_server_url, headers=headers)
        response.raise_for_status()
        return response.text
    except httpx.RequestError as e:
        logger.error(f"获取 {audience_url} 的认证令牌失败: {e}")
        # 在无法获取令牌时，可以选择失败或继续（取决于服务是否允许未经身份验证的调用）
        raise RuntimeError(f"无法为 {audience_url} 获取认证令牌") from e

# --- 数据引擎处理函数 ---

async def run_financials_batch(client: httpx.AsyncClient, tickers: List[str]):
    """调用财报引擎的批量刷新接口"""
    url = f"{settings.financial_engine_url}/batch_refresh"
    payload = {"tickers": tickers}
    
    logger.info(f"开始调用财报引擎: {url} for {len(tickers)} tickers...")
    token = await get_auth_token(client, settings.financial_engine_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        response = await client.post(url, json=payload, headers=headers, timeout=settings.request_timeout)
        response.raise_for_status()
        logger.info(f"财报引擎成功完成。响应: {response.json()}")
    except httpx.HTTPStatusError as e:
        logger.error(f"财报引擎请求失败: 状态码 {e.response.status_code}, 响应: {e.response.text}")
        raise
    except httpx.RequestError as e:
        logger.error(f"调用财报引擎时发生网络错误: {e}")
        raise

async def run_trading_batch(client: httpx.AsyncClient, tickers: List[str]):
    """调用交易数据引擎的批量刷新接口"""
    url = f"{settings.trading_engine_url}/trading_data/batch_refresh"
    
    logger.info(f"开始调用交易数据引擎: {url} for {len(tickers)} tickers...")
    token = await get_auth_token(client, settings.trading_engine_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    
    try:
        # 该接口要求 tickers 在 body 中以 embed=True 的形式
        response = await client.post(url, json={"tickers": tickers}, headers=headers, timeout=settings.request_timeout)
        response.raise_for_status()
        logger.info(f"交易数据引擎成功完成。响应: {response.json()}")
    except httpx.HTTPStatusError as e:
        logger.error(f"交易数据引擎请求失败: 状态码 {e.response.status_code}, 响应: {e.response.text}")
        raise
    except httpx.RequestError as e:
        logger.error(f"调用交易数据引擎时发生网络错误: {e}")
        raise

async def run_news_batch(client: httpx.AsyncClient, targets: List[Dict[str, Any]]):
    """顺序调用新闻爬虫引擎，支持股票 ticker 与专题目标"""
    logger.info(f"开始为 {len(targets)} 个新闻目标顺序调用新闻引擎...")
    if targets:
        preview = ", ".join(
            f"{item.get('ticker')}[{(item.get('target_type') or ('topic' if item.get('is_topic') else 'equity'))}]"
            for item in targets[:10]
        )
        if len(targets) > 10:
            preview += f", ... (total {len(targets)})"
        logger.info(f"新闻引擎目标示例: {preview}")
        for idx, item in enumerate(targets, start=1):
            logger.info(
                "新闻目标 #%d -> ticker=%s, type=%s, category=%s, date=%s",
                idx,
                item.get("ticker"),
                item.get("target_type") or ("topic" if item.get("is_topic") else "equity"),
                item.get("category"),
                item.get("date"),
            )

    token = await get_auth_token(client, settings.news_engine_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # 新闻引擎是按目标单个处理，所以我们顺序调用
    for target in targets:
        ticker = target.get("ticker")
        if not ticker:
            continue

        target_type = (target.get("target_type") or ("topic" if target.get("is_topic") else "equity")).lower()
        category = target.get("category")
        try:
            if target_type == "person":
                person_key = target.get("target_id") or ticker
                if not person_key:
                    logger.warning(f"  -> 跳过人物目标 {ticker}，缺少 person_key。")
                    continue
                url = f"{settings.news_engine_url}/batch/process_person/dynamic/{person_key}"
                logger.info(f"  -> 正在按人物处理新闻: {person_key} -> {url}")
                response = await client.post(
                    url,
                    json={"date": target.get("date")},
                    headers=headers,
                    timeout=settings.request_timeout,
                )
                response.raise_for_status()
                logger.info(f"  <- 成功按人物处理新闻: {person_key}。")
            elif target_type == "topic":
                topic_key = target.get("topic_key") or _slugify_segment(str(ticker))
                if not topic_key:
                    logger.warning(f"  -> 跳过目标 {ticker}，无法构造动态主题路径。")
                    continue
                url = f"{settings.news_engine_url}/batch/process_topic/dynamic/{topic_key}"
                logger.info(f"  -> 正在按主题处理新闻 (dynamic): {ticker} -> {url}")
                response = await client.post(
                    url,
                    json={"date": target.get("date")},
                    headers=headers,
                    timeout=settings.request_timeout,
                )
                response.raise_for_status()
                logger.info(f"  <- 成功按主题处理新闻: {ticker}。")
            else:
                url = f"{settings.news_engine_url}/batch/process_ticker/{ticker}"
                payload = {"date": None}
                logger.info(f"  -> 正在处理新闻: {ticker}")
                response = await client.post(url, json=payload, headers=headers, timeout=settings.request_timeout)
                response.raise_for_status()
                logger.info(f"  <- 成功处理新闻: {ticker}。")
        except httpx.HTTPStatusError as e:
            logger.error(f"处理新闻 {ticker} 失败: 状态码 {e.response.status_code}, 响应: {e.response.text}")
            # 单个 ticker/主题 失败，记录错误并继续处理下一个
            continue
        except httpx.RequestError as e:
            logger.error(f"调用新闻引擎处理 {ticker} 时发生网络错误: {e}")
            continue

    logger.info("新闻引擎批量处理完成。")


# --- QA 引擎处理函数 ---

async def run_qa_batch(
    client: httpx.AsyncClient, 
    tickers: List[str], 
    card_types: List[str],
    llm_config: Dict[str, Any],
    additional_targets: Optional[List[Dict[str, Any]]] = None,
):
    """批量调用 QA 引擎生成所有图卡"""
    logger.info(f"准备为 {len(tickers)} 个股票和 {len(card_types)} 种类型生成图卡...")


    token = await get_auth_token(client, settings.qa_engine_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    
    tasks = []
    default_config = llm_config.get("default", {})
    default_backend = default_config.get("backend")
    default_model = default_config.get("model_name")
    task_overrides = llm_config.get("tasks", {})

    processed_pairs = set()

    for ticker in tickers:
        for card_type in card_types:
            processed_pairs.add((ticker, card_type))
            card_override = task_overrides.get(card_type, {})
            final_backend = card_override.get("backend", default_backend)
            final_model = card_override.get("model_name", default_model)
            tasks.append(
                generate_single_card(
                    client,
                    ticker,
                    card_type,
                    headers,
                    final_backend,
                    final_model,
                )
            )

    extra_targets = additional_targets or []
    if extra_targets:
        logger.info(f"额外目标数量: {len(extra_targets)}")

    for target in extra_targets:
        target_ticker = target.get("ticker")
        if not target_ticker:
            continue

        target_card_types = target.get("card_types") or card_types
        extra_params = target.get("extra_params") or {}
        if not isinstance(extra_params, dict):
            extra_params = {}
        target_date = target.get("date")
        if target_date and "date_str" not in extra_params:
            extra_params = {**extra_params, "date_str": target_date}
        target_label = target.get("target_id") or target_ticker
        target_category = target.get("category")

        for card_type in target_card_types:
            pair_key = (target_ticker, card_type)
            if pair_key in processed_pairs:
                continue
            processed_pairs.add(pair_key)
            card_override = task_overrides.get(card_type, {})
            final_backend = card_override.get("backend", default_backend)
            final_model = card_override.get("model_name", default_model)
            tasks.append(
                generate_single_card(
                    client,
                    target_ticker,
                    card_type,
                    headers,
                    final_backend,
                    final_model,
                    extra_params=extra_params,
                    log_meta={"target": target_label, "category": target_category},
                )
            )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    success_count = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "success")
    failure_count = len(results) - success_count
    
    logger.info(f"QA 图卡生成批量完成。成功: {success_count}, 失败: {failure_count}。")
    if failure_count > 0:
        logger.warning("部分图卡生成失败，请检查上面的日志。")



async def generate_single_card(
    client: httpx.AsyncClient, 
    ticker: str, 
    card_type: str, 
    headers: Dict,
    backend: Optional[str],
    model_name: Optional[str],
    extra_params: Optional[Dict[str, Any]] = None,
    log_meta: Optional[Dict[str, Any]] = None,
):
    """为单个 ticker 和 card_type 调用 QA 引擎"""
    url = f"{settings.qa_engine_url}/card/{card_type}"
    params: Dict[str, Any] = {"ticker": ticker}
    if extra_params:
        params.update(extra_params)
    if "date_str" not in params:
        params["date_str"] = None
    if backend and "preferred_llm_backend" not in params:
        params["preferred_llm_backend"] = backend
    if model_name and "model_name" not in params:
        params["model_name"] = model_name
    
    try:
        backend_name = params.get("preferred_llm_backend", backend or "default")
        model_used = params.get("model_name", model_name or "default")
        log_parts = [f"  -> 正在生成图卡: {ticker} - {card_type}"]
        if log_meta:
            if log_meta.get("target"):
                log_parts.append(f"(Target: {log_meta['target']})")
            if log_meta.get("category"):
                log_parts.append(f"[Category: {log_meta['category']}]")
        log_parts.append(f"(Backend: {backend_name}, Model: {model_used})")
        logger.info(" ".join(log_parts))
        response = await client.post(url, params=params, headers=headers, timeout=settings.request_timeout)
        response.raise_for_status()
        logger.info(f"  <- 成功生成图卡: {ticker} - {card_type}")
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"生成图卡 {ticker}-{card_type} 失败: 状态码 {e.response.status_code}, 响应: {e.response.text}")
        return e
    except httpx.RequestError as e:
        logger.error(f"调用 QA 引擎生成 {ticker}-{card_type} 时发生网络错误: {e}")
        return e

# --- 主函数 ---

async def main():
    """Job 的主入口点"""
    logger.info("====== 开始执行批量编排 Job ======")

    try:
        # --- 修改：加载配置 ---
        tickers = load_gcs_json(settings.gcs_bucket_name, settings.ticker_list_path)
        card_types_config = load_gcs_json(settings.gcs_bucket_name, settings.card_types_path)
        raw_targets_config = load_gcs_json(settings.gcs_bucket_name, settings.targets_path)

        # --- 新增：加载引擎控制配置 ---
        engine_control_config = load_gcs_json(settings.gcs_bucket_name, settings.engine_control_path)
        llm_config = load_gcs_json(settings.gcs_bucket_name, settings.llm_config_path) or {}

        if not card_types_config:
            logger.error("图卡类型配置为空或加载失败，Job 终止。")
            return
        
        # --- 新增逻辑：筛选出启用的 card_types ---
        enabled_card_types = [
            item["name"] for item in card_types_config if isinstance(item, dict) and item.get("enabled")
        ]
        enabled_card_type_set = set(enabled_card_types)
        
        if not enabled_card_types:
            logger.warning("在配置文件中没有找到任何启用的图卡类型，QA 阶段将被跳过。")

        logger.info(f"本次将处理 {len(enabled_card_types)} 种启用的图卡类型: {enabled_card_types}")
        
        additional_targets, equities_settings = _expand_card_targets(
            raw_targets_config, enabled_card_types, settings.gcs_bucket_name
        )

        equities_card_types_config = equities_settings.get("card_types") or []
        equities_card_types = [ct for ct in equities_card_types_config if ct in enabled_card_type_set]
        if equities_card_types_config and not equities_card_types:
            logger.warning(
                "equities_default 中配置的 card_types 均已被禁用: %s",
                equities_card_types_config,
            )

        if not equities_card_types:
            preferred_equity_types = [
                "news_card",
                "earnings_card",
                "trading_card",
                "news_bull_bear_card",
                "daily_summary_card",
            ]
            equities_card_types = [ct for ct in preferred_equity_types if ct in enabled_card_type_set]

        if not equities_card_types:
            equities_card_types = list(enabled_card_types)

        logger.info(f"股票默认图卡类型: {equities_card_types}")

        equities_run_overrides = dict(equities_settings.get("run_overrides") or {})

        if additional_targets:
            logger.info(f"从 card_targets.json 获取到 {len(additional_targets)} 个额外目标。")
        if not tickers and not additional_targets:
            logger.error("股票列表与 card_targets 均为空，Job 终止。")
            return

        # --- 新增：设置默认的引擎控制 ---
        # 如果文件不存在或不是字典，则默认全部运行
        if not isinstance(engine_control_config, dict):
            engine_control_config = {
                "run_financials_engine": True,
                "run_trading_engine": True,
                "run_news_engine": True,
            }
            logger.warning("引擎控制文件加载失败或格式错误，将默认运行所有数据引擎。")

    except Exception:
        logger.error("无法加载 GCS 配置文件，Job 终止。")
        return

    async with httpx.AsyncClient() as client:
        # --- 阶段一: 并发处理数据引擎 ---
        logger.info("--- 阶段一: 开始处理数据引擎 ---")
        tickers_for_financials: Set[str] = set()
        tickers_for_trading: Set[str] = set()
        news_target_map: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        def upsert_news_target(
            *,
            ticker: str,
            target_type: str,
            category: Optional[str],
            date: Optional[str],
            is_topic: Optional[bool] = None,
            target_id: Optional[str] = None,
            topic_key: Optional[str] = None,
        ) -> None:
            """Store (or replace) a news target entry keyed by ticker/type/category."""
            normalized_type = (target_type or "equity").lower()
            resolved_is_topic = is_topic if is_topic is not None else normalized_type == "topic"
            resolved_category = category or ("topic" if resolved_is_topic else "equity")
            key = (ticker, normalized_type, resolved_category.lower())
            news_target_map[key] = {
                "ticker": ticker,
                "category": resolved_category,
                "is_topic": resolved_is_topic,
                "target_type": normalized_type,
                "date": date,
                "target_id": target_id,
                "topic_key": topic_key,
            }
            logger.info(
                "已记录新闻目标 -> ticker=%s, target_type=%s, category=%s, date=%s",
                ticker,
                normalized_type,
                resolved_category,
                date,
            )

        base_tickers = list(tickers or [])
        base_ticker_set = set(base_tickers)
        equities_override_defaults = dict(equities_run_overrides or {})

        equity_targets_by_ticker: Dict[str, Dict[str, Any]] = {
            tgt["ticker"]: tgt
            for tgt in additional_targets
            if isinstance(tgt, dict) and tgt.get("ticker") and (tgt.get("target_type") or "").lower() == "equity"
        }
        processed_equity_overrides: Set[str] = set()

        for ticker in base_tickers:
            overrides: Dict[str, Any] = {}
            overrides.update(equities_override_defaults)

            override_target = equity_targets_by_ticker.get(ticker)
            override_category: Optional[str] = None
            override_date: Optional[str] = None
            override_target_id: Optional[str] = None
            override_topic_key: Optional[str] = None
            if override_target:
                processed_equity_overrides.add(ticker)
                overrides.update(override_target.get("run_overrides") or {})
                override_category = override_target.get("category")
                override_date = override_target.get("date")
                override_target_id = override_target.get("target_id")
                override_topic_key = override_target.get("topic_key")

            flags = _resolve_engine_flags(engine_control_config, overrides)
            if flags.get("financials"):
                tickers_for_financials.add(ticker)
            if flags.get("trading"):
                tickers_for_trading.add(ticker)
            if flags.get("news"):
                upsert_news_target(
                    ticker=ticker,
                    target_type="equity",
                    category=override_category or "equity",
                    date=override_date,
                    is_topic=False,
                    target_id=override_target_id or ticker,
                    topic_key=override_topic_key,
                )

        for target in additional_targets:
            if not isinstance(target, dict):
                continue
            ticker = target.get("ticker")
            if not ticker:
                continue

            raw_target_type = target.get("target_type") or ("topic" if target.get("is_topic") else "equity")
            target_type = raw_target_type.lower()

            if target_type == "equity" and ticker in processed_equity_overrides:
                # 已经在默认股票批次中处理过该 equity 目标。
                continue

            overrides: Dict[str, Any] = {}
            if target_type == "equity" and ticker in base_ticker_set:
                overrides.update(equities_override_defaults)
            overrides.update(target.get("run_overrides") or {})

            flags = _resolve_engine_flags(engine_control_config, overrides)
            if flags.get("financials"):
                tickers_for_financials.add(ticker)
            if flags.get("trading"):
                tickers_for_trading.add(ticker)
            if flags.get("news"):
                upsert_news_target(
                    ticker=ticker,
                    target_type=target_type,
                    category=target.get("category"),
                    date=target.get("date"),
                    is_topic=target.get("is_topic"),
                    target_id=target.get("target_id"),
                    topic_key=target.get("topic_key"),
                )

        data_engine_tasks: List[Any] = []

        if tickers_for_financials:
            data_engine_tasks.append(("Financials", run_financials_batch(client, sorted(tickers_for_financials))))
            logger.info("  -> 财报引擎已加入执行队列，共 %d 个 ticker。", len(tickers_for_financials))
        else:
            logger.info("  -> 根据配置，跳过财报引擎。")

        if tickers_for_trading:
            data_engine_tasks.append(("Trading", run_trading_batch(client, sorted(tickers_for_trading))))
            logger.info("  -> 交易数据引擎已加入执行队列，共 %d 个 ticker。", len(tickers_for_trading))
        else:
            logger.info("  -> 根据配置，跳过交易数据引擎。")

        news_targets = list(news_target_map.values())
        if news_targets:
                # 稍微稳定顺序，便于日志追踪
            news_targets.sort(
                key=lambda item: (
                    item.get("is_topic", False),
                    item.get("category") or "",
                    item.get("ticker") or "",
                )
            )
            logger.info("新闻引擎最终目标列表 (%d):", len(news_targets))
            for idx, item in enumerate(news_targets, start=1):
                target_type = item.get("target_type") or (
                    "topic" if item.get("is_topic") else "equity"
                )
                logger.info(
                    "  [%d] ticker=%s, type=%s, category=%s, date=%s",
                    idx,
                    item.get("ticker"),
                    target_type,
                    item.get("category"),
                    item.get("date"),
                )
            data_engine_tasks.append(("News", run_news_batch(client, news_targets)))
            logger.info("  -> 新闻引擎已加入执行队列，共 %d 个目标。", len(news_targets))
        else:
            logger.info("  -> 根据配置，跳过新闻引擎。")
        

        # 只有当任务列表不为空时，才执行
        if data_engine_tasks:
            results = await asyncio.gather(*(task for _, task in data_engine_tasks), return_exceptions=True)
            # 检查是否有任何数据引擎任务失败
            if any(isinstance(res, Exception) for res in results):
                logger.error("一个或多个数据引擎处理失败，Job 终止，不会触发 QA 引擎。")
                # 记录具体的异常信息
                for (task_name, _), res in zip(data_engine_tasks, results):
                    if isinstance(res, Exception):
                        logger.error(f"  -> 失败的任务: {task_name}, 异常: {res}")
                return # 提前退出
            logger.info("--- 所有数据引擎处理成功 ---")
        else:
            logger.info("--- 没有需要运行的数据引擎，直接进入下一阶段 ---")


        # --- 阶段二: 批量生成图卡 ---
        tickers_for_qa = tickers or []

        if equities_card_types or additional_targets:
            logger.info("--- 阶段二: 开始调用 QA 引擎生成图卡 ---")
            if not equities_card_types and tickers_for_qa:
                logger.warning("股票默认图卡类型列表为空，将跳过股票批次，仅处理额外目标。")
            await run_qa_batch(
                client,
                tickers_for_qa,
                equities_card_types,
                llm_config,
                additional_targets,
            )
        else:
            logger.info("--- 阶段二: 没有启用的图卡类型，跳过 QA 引擎调用 ---")

    logger.info("====== 批量编排 Job 执行完毕 ======")


if __name__ == "__main__":
    asyncio.run(main())
