"""
financial-engine / main.py
=================================

本服务是 **VCard 项目 - 财报数据引擎** 的实现，职责聚焦：

1) **拉取财报**：基于 yfinance 获取单只股票的财务三大表（年/季）、earnings（年/季）和公司信息。
2) **无条件增量合并**：不做“最新日期”的判断；每次批量/刷新都直接取最新全量，再与历史 JSON 按 `index` 去重合并，避免漏更/错更。
3) **语义摘要**：对关键财务指标与估值做简要中文解读，供下游 AI 使用。
4) **AI Context 落盘**：按 **America/Los_Angeles** 时区为每只股票每天生成一份 `ai_context/{TICKER}/{YYYY-MM-DD}.txt`。
5) **当日清单**：同步维护 `ai_context/daily_index/{YYYY-MM-DD}.json`，供 card-job 批量取数。
6) **接口解耦**：同时提供“批量清单接口”和“按 ticker 取 path 接口”，QA/卡片不需要自己拼路径。

注意：
- 该服务每次运行都“全量拉取 + 去重合并”，没有任何“最新日期”的网络判断，
  目的是 **避免Yahoo端数据回填/修订导致的遗漏**（您已明确要求）。
- yfinance 的 DataFrame 列为财务科目，索引为日期；保存前会统一转为 JSON 友好的 records 列表，`index` 为字符串。
- 合并规则：同一个 `index`（日期）以“新数据覆盖旧数据”；最终按日期倒序（新→旧）。

部署/运行见 README.md。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, date
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Union
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import pytz
import yfinance as yf
import numpy as np  # 确保导入 numpy

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from google.cloud import storage

from report_source import ReportSourceService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Prepare data directory for saved JSON files (for local development/testing)
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# GCS 桶名称与时区
# 强烈建议通过环境变量来设置桶名称，以便灵活配置。
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "financial-data-engine-bucket")
ENGINE_TZ = os.environ.get("ENGINE_TZ", "America/Los_Angeles")
REPORT_SOURCE_PREFIX = os.environ.get("REPORT_SOURCE_PREFIX", "report_sources")
REPORT_SOURCE_CACHE_TTL_SECONDS = int(os.environ.get("REPORT_SOURCE_CACHE_TTL_SECONDS", "86400"))
REPORT_SOURCE_MAX_CANDIDATES = int(os.environ.get("REPORT_SOURCE_MAX_CANDIDATES", "24"))
tz = pytz.timezone(ENGINE_TZ)
# Create FastAPI app
app = FastAPI(
    title="Enhanced Financial Data Service",
    description="提供完整的财务报表获取、保存、语义解读及定时更新服务。",
    version="1.1.0",
)


class FinancialData(BaseModel):
    """
    完整的财务数据模型，用于序列化返回。
    """
    ticker: str
    annual_financials: List[Dict] = Field(default_factory=list)
    annual_balance_sheet: List[Dict] = Field(default_factory=list)
    annual_cashflow: List[Dict] = Field(default_factory=list)
    quarterly_financials: List[Dict] = Field(default_factory=list)
    quarterly_balance_sheet: List[Dict] = Field(default_factory=list)
    quarterly_cashflow: List[Dict] = Field(default_factory=list)
    annual_earnings: List[Dict] = Field(default_factory=list)
    quarterly_earnings: List[Dict] = Field(default_factory=list)
    info: Dict[str, Any] = Field(default_factory=dict)
    valuations: Dict[str, Optional[float]] = Field(default_factory=dict)
    fetched_at: datetime


# 财报缓存：{ ticker: { 'interpretation_data': Dict, 'interpretations': List[str], 'last_updated': iso, 'saved_file_path': Optional[str], 'ai_context_path': Optional[str], 'daily_index_path': Optional[str] } }
earnings_cache: Dict[str, Dict] = {}
_report_source_service: Optional[ReportSourceService] = None

# --- In-process de-dup / L1 cache (per Cloud Run instance, best effort) ---
_FINANCIAL_LOCK = Lock()
_FINANCIAL_INFLIGHT: Dict[str, Event] = {}
_FINANCIAL_L1_CACHE: Dict[str, Dict[str, Any]] = {}
_FINANCIAL_L1_HIT_TTL_SECONDS = int(os.environ.get("FINANCIAL_L1_HIT_TTL_SECONDS", "600"))  # 10 min
_FINANCIAL_L1_MISS_TTL_SECONDS = int(os.environ.get("FINANCIAL_L1_MISS_TTL_SECONDS", "120"))  # 2 min
_FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS = int(
    os.environ.get("FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS", "3")
)

# Trading service endpoint (used as source-of-truth for earnings day schedule).
TRADING_DATA_ENGINE_URL = os.environ.get("TRADING_DATA_ENGINE_URL", "").strip()


def _get_report_source_service() -> ReportSourceService:
    global _report_source_service
    if _report_source_service is None:
        _report_source_service = ReportSourceService(
            bucket_name=GCS_BUCKET_NAME,
            local_data_dir=DATA_DIR,
            prefix=REPORT_SOURCE_PREFIX,
            cache_ttl_seconds=REPORT_SOURCE_CACHE_TTL_SECONDS,
            max_candidates=REPORT_SOURCE_MAX_CANDIDATES,
        )
    return _report_source_service


def _today_str() -> str:
    """获取当前日期字符串，基于配置的时区 (ENGINE_TZ)。"""
    return datetime.now(tz).strftime("%Y-%m-%d")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt
    except Exception:
        return None


def _parse_iso_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if len(s) >= 10:
            return date.fromisoformat(s[:10])
    except Exception:
        return None
    return None


def _get_financial_blob_name(ticker: str) -> str:
    return f"{ticker.upper()}_financials.json"


def _get_local_financial_filepath(ticker: str) -> str:
    return os.path.join(DATA_DIR, f"{ticker.upper()}_financials.json")


def _load_financials_from_storage(ticker: str) -> Optional[Dict[str, Any]]:
    t = ticker.upper()
    blob_name = _get_financial_blob_name(t)
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(blob_name)
            if blob.exists():
                raw = blob.download_as_text(encoding="utf-8")
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return obj
        except Exception as exc:
            logger.error("Failed to load financial data for %s from GCS: %s", t, exc)

    local_path = _get_local_financial_filepath(t)
    if not os.path.exists(local_path):
        return None
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception as exc:
        logger.error("Failed to load financial data for %s from local fallback: %s", t, exc)
        return None


def _set_financial_to_l1(ticker: str, payload: Dict[str, Any]) -> None:
    t = ticker.upper()
    has_payload = bool(payload)
    ttl = _FINANCIAL_L1_HIT_TTL_SECONDS if has_payload else _FINANCIAL_L1_MISS_TTL_SECONDS
    with _FINANCIAL_LOCK:
        _FINANCIAL_L1_CACHE[t] = {
            "payload": dict(payload),
            "expiresAt": time.time() + float(ttl),
        }


def _get_financial_from_l1(ticker: str) -> Optional[Dict[str, Any]]:
    t = ticker.upper()
    now = time.time()
    with _FINANCIAL_LOCK:
        entry = _FINANCIAL_L1_CACHE.get(t)
        if not entry:
            return None
        if now >= float(entry.get("expiresAt", 0)):
            _FINANCIAL_L1_CACHE.pop(t, None)
            return None
        payload = entry.get("payload")
        return dict(payload) if isinstance(payload, dict) else None


def _fetch_next_earnings_date_from_trading_service(ticker: str) -> Optional[date]:
    if not TRADING_DATA_ENGINE_URL:
        return None
    t = ticker.upper()
    try:
        qs = urlencode({"symbol": t, "force_refresh": 0})
        url = f"{TRADING_DATA_ENGINE_URL.rstrip('/')}/api/market/earnings/next?{qs}"
        req = Request(url, headers={"accept": "application/json"})
        with urlopen(req, timeout=3.0) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        return _parse_iso_date(obj.get("nextEarningsDate"))
    except (URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch next earnings date for %s from trading service: %s", t, exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected error when fetching next earnings date for %s: %s", t, exc)
        return None


def _last_refresh_date_from_payload(payload: Dict[str, Any]) -> Optional[date]:
    if not isinstance(payload, dict):
        return None

    cache_meta = payload.get("cache_meta")
    if isinstance(cache_meta, dict):
        dt = _parse_iso_datetime(cache_meta.get("last_refreshed_at"))
        if dt is not None:
            return dt.astimezone(tz).date()

    dt = _parse_iso_datetime(payload.get("fetched_at"))
    if dt is not None:
        return dt.astimezone(tz).date()

    latest_dates: List[date] = []
    for key in ("quarterly_earnings", "quarterly_financials", "annual_earnings", "annual_financials"):
        rows = payload.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        for row in rows[:4]:
            if not isinstance(row, dict):
                continue
            d = _parse_iso_date(row.get("date"))
            if d is not None:
                latest_dates.append(d)
                break
    return max(latest_dates) if latest_dates else None


def _cached_next_earnings_date_from_payload(payload: Dict[str, Any]) -> Optional[date]:
    if not isinstance(payload, dict):
        return None
    cache_meta = payload.get("cache_meta")
    if isinstance(cache_meta, dict):
        return _parse_iso_date(cache_meta.get("next_earnings_date"))
    return None


def _should_refresh_financials(
    cached_payload: Optional[Dict[str, Any]],
    next_earnings_date: Optional[date],
    force_refresh: bool = False,
) -> tuple[bool, str]:
    if force_refresh:
        return True, "force_refresh"
    if not isinstance(cached_payload, dict) or not cached_payload:
        return True, "cold_start"

    last_refresh_date = _last_refresh_date_from_payload(cached_payload)
    if last_refresh_date is None:
        return True, "missing_last_refresh_date"

    cached_next_earnings_date = _cached_next_earnings_date_from_payload(cached_payload)
    # If trading service has no date yet, keep cached data and avoid extra yfinance traffic.
    today_local = datetime.now(tz).date()

    # Prefer the boundary captured in last successful refresh.
    # This directly solves: "previous earnings day already passed but no refresh yet".
    if cached_next_earnings_date is not None:
        if today_local < cached_next_earnings_date:
            return False, "before_cached_earnings_day"
        if last_refresh_date < cached_next_earnings_date:
            return True, "cached_earnings_day_passed"
        if next_earnings_date is not None and next_earnings_date <= cached_next_earnings_date:
            return False, "already_refreshed_after_cached_earnings"

    if next_earnings_date is None:
        stale_days = (today_local - last_refresh_date).days
        if stale_days >= max(1, _FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS):
            return True, "no_earnings_date_stale_timeout"
        return False, "no_earnings_date_recent"

    if today_local < next_earnings_date:
        return False, "before_earnings_day"
    if last_refresh_date >= next_earnings_date:
        return False, "already_refreshed_after_earnings"
    return True, "earnings_day_passed"


def _records_to_interpretation_dict(records: Any) -> Dict[str, List[Any]]:
    if not isinstance(records, list):
        return {}
    valid_rows = [r for r in records if isinstance(r, dict)]
    if not valid_rows:
        return {}
    try:
        valid_rows.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    except Exception:
        pass
    out: Dict[str, List[Any]] = {}
    for row in valid_rows:
        for k, v in row.items():
            if k == "date":
                continue
            out.setdefault(str(k), []).append(v)
    return out


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


def _ratio(numerator: Any, denominator: Any) -> Optional[float]:
    n = _safe_float(numerator)
    d = _safe_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def _pct_change(current: Any, previous: Any) -> Optional[float]:
    cur = _safe_float(current)
    prev = _safe_float(previous)
    if cur is None or prev is None or prev == 0:
        return None
    return ((cur - prev) / abs(prev)) * 100.0


def _score_linear(value: Optional[float], lower: float, upper: float, invert: bool = False) -> Optional[float]:
    if value is None or upper <= lower:
        return None
    clipped = min(max(value, lower), upper)
    s = ((clipped - lower) / (upper - lower)) * 2.0 - 1.0
    if invert:
        s = -s
    return float(min(1.0, max(-1.0, s)))


def _mean_score(values: List[Optional[float]]) -> tuple[float, int, int]:
    total = len(values)
    valid = [v for v in values if v is not None]
    if not valid:
        return 0.0, 0, total
    return float(sum(valid) / len(valid)), len(valid), total


def _sorted_record_list(financial_obj: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    rows = financial_obj.get(key)
    if not isinstance(rows, list):
        return []
    valid_rows = [r for r in rows if isinstance(r, dict)]
    try:
        valid_rows.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    except Exception:
        pass
    return valid_rows


def _metric_from_row(row: Optional[Dict[str, Any]], candidates: List[str]) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    for key in candidates:
        value = _safe_float(row.get(key))
        if value is not None:
            return value
    return None


def _signal_from_score(score: float) -> str:
    if score >= 0.20:
        return "bullish"
    if score <= -0.20:
        return "bearish"
    return "neutral"


def _compute_fundamental_factor_signal(financial_obj: Dict[str, Any]) -> Dict[str, Any]:
    q_fin = _sorted_record_list(financial_obj, "quarterly_financials")
    q_bal = _sorted_record_list(financial_obj, "quarterly_balance_sheet")
    q_cf = _sorted_record_list(financial_obj, "quarterly_cashflow")
    q_earn = _sorted_record_list(financial_obj, "quarterly_earnings")
    vals = financial_obj.get("valuations") if isinstance(financial_obj.get("valuations"), dict) else {}

    fin0 = q_fin[0] if len(q_fin) > 0 else None
    fin1 = q_fin[1] if len(q_fin) > 1 else None
    fin4 = q_fin[4] if len(q_fin) > 4 else None
    bal0 = q_bal[0] if len(q_bal) > 0 else None
    cf0 = q_cf[0] if len(q_cf) > 0 else None
    cf1 = q_cf[1] if len(q_cf) > 1 else None
    earn0 = q_earn[0] if len(q_earn) > 0 else None
    earn1 = q_earn[1] if len(q_earn) > 1 else None
    earn4 = q_earn[4] if len(q_earn) > 4 else None

    revenue0 = _metric_from_row(fin0, ["Total Revenue", "Revenue"]) or _metric_from_row(earn0, ["Revenue"])
    revenue1 = _metric_from_row(fin1, ["Total Revenue", "Revenue"]) or _metric_from_row(earn1, ["Revenue"])
    revenue4 = _metric_from_row(fin4, ["Total Revenue", "Revenue"]) or _metric_from_row(earn4, ["Revenue"])
    eps0 = _metric_from_row(earn0, ["Earnings", "Diluted EPS", "Basic EPS"])
    eps1 = _metric_from_row(earn1, ["Earnings", "Diluted EPS", "Basic EPS"])
    eps4 = _metric_from_row(earn4, ["Earnings", "Diluted EPS", "Basic EPS"])

    gross_profit0 = _metric_from_row(fin0, ["Gross Profit"])
    gross_profit1 = _metric_from_row(fin1, ["Gross Profit"])
    operating_income0 = _metric_from_row(fin0, ["Operating Income"])
    net_income0 = _metric_from_row(fin0, ["Net Income"])
    net_income1 = _metric_from_row(fin1, ["Net Income"])
    fcf0 = _metric_from_row(cf0, ["Free Cash Flow"])
    fcf1 = _metric_from_row(cf1, ["Free Cash Flow"])
    ocf0 = _metric_from_row(cf0, ["Operating Cash Flow"])

    total_debt0 = _metric_from_row(bal0, ["Total Debt"])
    equity0 = _metric_from_row(bal0, ["Common Stock Equity", "Stockholders Equity", "Total Equity Gross Minority Interest"])
    cash0 = _metric_from_row(bal0, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"])
    current_assets0 = _metric_from_row(bal0, ["Current Assets"])
    current_liabilities0 = _metric_from_row(bal0, ["Current Liabilities", "Current Liabilities Net Minority Interest"])

    revenue_qoq = _pct_change(revenue0, revenue1)
    revenue_yoy = _pct_change(revenue0, revenue4)
    eps_qoq = _pct_change(eps0, eps1)
    eps_yoy = _pct_change(eps0, eps4)
    fcf_qoq = _pct_change(fcf0, fcf1)

    gross_margin = _ratio(gross_profit0, revenue0)
    gross_margin_prev = _ratio(gross_profit1, revenue1)
    operating_margin = _ratio(operating_income0, revenue0)
    net_margin = _ratio(net_income0, revenue0)
    net_margin_prev = _ratio(net_income1, revenue1)
    gross_margin_delta = None if gross_margin is None or gross_margin_prev is None else (gross_margin - gross_margin_prev)
    net_margin_delta = None if net_margin is None or net_margin_prev is None else (net_margin - net_margin_prev)

    fcf_margin = _ratio(fcf0, revenue0)
    ocf_to_net_income = _ratio(ocf0, net_income0)

    debt_to_equity = _ratio(total_debt0, equity0)
    cash_to_debt = _ratio(cash0, total_debt0)
    current_ratio = _ratio(current_assets0, current_liabilities0)

    trailing_pe = _safe_float(vals.get("trailing_pe"))
    price_to_sales = _safe_float(vals.get("price_to_sales"))
    price_to_book = _safe_float(vals.get("price_to_book"))

    factor_weights = {
        "growth": 0.34,
        "profitability": 0.24,
        "cashflow_quality": 0.22,
        "balance_sheet": 0.14,
        "valuation": 0.06,
    }

    factor_metric_scores: Dict[str, List[Optional[float]]] = {
        "growth": [
            _score_linear(revenue_qoq, -15.0, 15.0),
            _score_linear(revenue_yoy, -30.0, 30.0),
            _score_linear(eps_qoq, -25.0, 25.0),
            _score_linear(eps_yoy, -40.0, 40.0),
        ],
        "profitability": [
            _score_linear(gross_margin, 0.20, 0.65),
            _score_linear(operating_margin, 0.05, 0.30),
            _score_linear(net_margin, 0.03, 0.22),
            _score_linear(gross_margin_delta, -0.03, 0.03),
            _score_linear(net_margin_delta, -0.02, 0.02),
        ],
        "cashflow_quality": [
            _score_linear(fcf_margin, 0.00, 0.20),
            _score_linear(ocf_to_net_income, 0.60, 1.60),
            _score_linear(fcf_qoq, -30.0, 30.0),
        ],
        "balance_sheet": [
            _score_linear(debt_to_equity, 0.20, 2.50, invert=True),
            _score_linear(cash_to_debt, 0.10, 1.20),
            _score_linear(current_ratio, 1.00, 2.50),
        ],
        "valuation": [
            _score_linear(trailing_pe, 10.0, 40.0, invert=True),
            _score_linear(price_to_sales, 1.0, 12.0, invert=True),
            _score_linear(price_to_book, 1.0, 10.0, invert=True),
        ],
    }

    factor_rows: List[Dict[str, Any]] = []
    total_score = 0.0
    available_metric_count = 0
    total_metric_count = 0

    for name, scores in factor_metric_scores.items():
        score, available, total = _mean_score(scores)
        weight = factor_weights.get(name, 0.0)
        contribution = score * weight
        total_score += contribution
        available_metric_count += available
        total_metric_count += total
        factor_rows.append(
            {
                "name": name,
                "weight": round(weight, 4),
                "score": round(score, 4),
                "contribution": round(contribution, 4),
                "available_metrics": available,
                "total_metrics": total,
            }
        )

    confidence = 0.0
    if total_metric_count > 0:
        confidence = min(1.0, max(0.0, available_metric_count / total_metric_count))

    contribution_map = {row["name"]: row["contribution"] for row in factor_rows}
    overall_score = float(min(1.0, max(-1.0, total_score)))
    signal = _signal_from_score(overall_score)

    derived_metrics = {
        "revenue_qoq_pct": revenue_qoq,
        "revenue_yoy_pct": revenue_yoy,
        "eps_qoq_pct": eps_qoq,
        "eps_yoy_pct": eps_yoy,
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "net_margin": net_margin,
        "gross_margin_delta": gross_margin_delta,
        "net_margin_delta": net_margin_delta,
        "fcf_margin": fcf_margin,
        "ocf_to_net_income": ocf_to_net_income,
        "fcf_qoq_pct": fcf_qoq,
        "debt_to_equity": debt_to_equity,
        "cash_to_debt": cash_to_debt,
        "current_ratio": current_ratio,
        "trailing_pe": trailing_pe,
        "price_to_sales": price_to_sales,
        "price_to_book": price_to_book,
    }

    return {
        "version": "fundamental_factor_v1",
        "overall": {
            "score": round(overall_score, 4),
            "signal": signal,
            "confidence": round(confidence, 4),
        },
        "factors": factor_rows,
        "factor_contributions": contribution_map,
        "derived_metrics": derived_metrics,
        "stockflow_input": {
            "fundamental_score": round(overall_score, 4),
            "confidence": round(confidence, 4),
            "signal": signal,
            "factor_contributions": contribution_map,
        },
    }


def _build_interpreted_payload_from_financial_obj(
    ticker: str,
    financial_obj: Dict[str, Any],
    cache_layer: str,
    stale: bool = False,
    stale_reason: str = "",
) -> Dict[str, Any]:
    t = ticker.upper()
    interpretation_data = {
        "financials": _records_to_interpretation_dict(financial_obj.get("quarterly_financials")),
        "earnings": _records_to_interpretation_dict(financial_obj.get("quarterly_earnings")),
        "valuations": financial_obj.get("valuations") if isinstance(financial_obj.get("valuations"), dict) else {},
        "info": financial_obj.get("info") if isinstance(financial_obj.get("info"), dict) else {},
    }
    interpretations = _interpret_financials(interpretation_data)
    fundamental_signal = _compute_fundamental_factor_signal(financial_obj)
    last_refresh = None
    cache_meta = financial_obj.get("cache_meta")
    if isinstance(cache_meta, dict):
        last_refresh = cache_meta.get("last_refreshed_at")
    if not last_refresh:
        last_refresh = financial_obj.get("fetched_at")
    payload = {
        "ticker": t,
        "interpretation_data": interpretation_data,
        "interpretations": interpretations,
        "last_updated": str(last_refresh) if last_refresh else datetime.now(tz).isoformat(),
        "saved_file_path": f"gs://{GCS_BUCKET_NAME}/{_get_financial_blob_name(t)}" if GCS_BUCKET_NAME else _get_local_financial_filepath(t),
        "ai_context_path": None,
        "daily_index_path": None,
        "cacheLayer": cache_layer,
        "fundamental_signal": fundamental_signal,
        "stockflow_input": fundamental_signal.get("stockflow_input"),
    }
    if isinstance(cache_meta, dict):
        payload["cache_meta"] = cache_meta
    if stale:
        payload["stale"] = True
        if stale_reason:
            payload["staleReason"] = stale_reason
    return payload


# --- 辅助：将 yfinance 的 DataFrame 转成 JSON 友好格式 ---
# 说明：
#  - 列为科目，索引多为日期。
#  - 我们把 DatetimeIndex 转成 ISO 字符串；
#  - 将 NaN -> None，避免 JSON 不可序列化；
#  - orient=records，最终每行都是 {'index': 'YYYY-MM-DD', 'Revenue': ...}。
# 【修改点 1：_convert_df_for_storage 函数】
# 改动原因：修正 DataFrame 转置逻辑，使其输出以日期为主键，指标为子键的记录列表。
#          这样便于后续 _generate_ai_context_text 函数按日期提取数据。
def _convert_df_for_storage(df: Optional[pd.DataFrame]) -> List[Dict]:
    """
    将 DataFrame 转换为 orient='records' 的列表，其中每个字典代表一个日期的所有指标数据。
    原始 DataFrame 的结构通常是指标作为索引，日期作为列。
    这里将其转置，使日期成为记录的键，指标为子键，便于按日期访问。
    """
    if df is None or df.empty:
        return []

    # YFinance的financials/balance_sheet/cashflow DataFrame的index是指标，columns是日期。
    # 我们需要将其转置，使日期成为行索引，指标成为列。
    df_transposed = df.T # Transpose the DataFrame

    # 将转置后的DataFrame的索引（现在是日期）转换为ISO格式的字符串
    if isinstance(df_transposed.index, pd.DatetimeIndex):
        # 使用 strftime 得到更简洁的日期字符串，例如 "YYYY-MM-DD"
        df_transposed.index = df_transposed.index.map(lambda x: x.strftime("%Y-%m-%d"))

    # 将索引重置为常规列，并命名为'date'以便清晰
    # 然后转换为 records 格式，这样每个记录的键就是日期，值就是该日期下的所有指标
    result = df_transposed.reset_index().rename(columns={'index': 'date'}).replace({np.nan: None}).to_dict(orient="records")

    # 确保记录按日期降序排列 (最新在前)
    try:
        # Assuming 'date' column contains ISO format strings which can be sorted lexicographically
        # This is often already sorted by yfinance, but explicit sort ensures consistency.
        result.sort(key=lambda x: x.get('date', ''), reverse=True)
    except Exception as e:
        logger.warning(f"Failed to sort records by date for storage: {e}")

    return result


# --- 辅助：用于 AI 解读的 DataFrame -> 字典（orient='list'） ---
# 说明：
#  - 生成给 _interpret_financials 使用的紧凑结构；
#  - 索引转字符串，lists 便于取最近两期比较。
# 【修改点 2：_convert_df_for_interpretation 函数】
# 改动原因：修正其输出格式，使其严格符合 {'MetricName': [latest_value, prev_value, ...]} 的预期。
def _convert_df_for_interpretation(df: Optional[pd.DataFrame]) -> Dict:
    """
    将 DataFrame 转换为适合语义解读的字典格式。
    返回的字典将以指标名称为键，以该指标在各个日期的值（从最新到最旧）的列表为值。
    例如: {'Total Revenue': [latest_value, prev_value, ...], 'Net Income': [latest_value, prev_value, ...]}
    """
    if df is None or df.empty:
        return {}

    # YFinance DataFrame (df) 结构通常是：
    #                    日期1       日期2
    # 财务指标A           值A1     值A2
    # 财务指标B           值B1     值B2
    #
    # 我们需要：`{'财务指标A': [值A1, 值A2], '财务指标B': [值B1, 值B2]}`
    # 并且 `[值A1, 值A2]` 是从最新到最旧。

    # 使用 to_dict(orient='index') 得到 {'MetricA': {Date1: V1, Date2: V2}, ...}
    temp_dict = df.to_dict(orient='index') 

    result_for_interp = {}
    # 获取所有日期列名并进行降序排序 (最新日期在前)
    sorted_dates = sorted(df.columns, reverse=True)

    for metric_name, date_values_dict in temp_dict.items():
        # 为每个指标，根据排序后的日期提取值
        values_list = [date_values_dict.get(date) for date in sorted_dates]
        result_for_interp[metric_name] = [v if not pd.isna(v) else None for v in values_list] # 将 NaN 转换为 None
        
    return result_for_interp


# ====== 辅助函数来深度清理数据以确保 JSON 可序列化 ======
def _deep_clean_json_serializable(data: Any) -> Any:
    """
    递归地清理数据结构，确保所有键是字符串，并将 datetime/Timestamp 和 NaN 值转换为 JSON 可序列化格式。
    """
    if isinstance(data, dict):
        cleaned_dict = {}
        for k, v in data.items():
            # 确保字典的键是字符串
            cleaned_key = str(k)
            # 递归清理值
            cleaned_dict[cleaned_key] = _deep_clean_json_serializable(v)
        return cleaned_dict
    elif isinstance(data, list):
        # 递归清理列表中的每个元素
        return [_deep_clean_json_serializable(item) for item in data]
    elif isinstance(data, (datetime, pd.Timestamp)):
        # 将 datetime 或 pandas.Timestamp 对象转换为 ISO 格式的字符串
        return data.isoformat()
    elif pd.isna(data):  # Checks for numpy.nan and pandas.NA
        # 将 pandas 的 NaN (Not a Number) 值转换为 None
        return None
    # 对于其他基本类型 (str, int, float, bool, None)，直接返回
    return data
# =============================================================


# --- 核心：抓取单支股票的完整财务数据（年/季/earnings/公司信息/估值） ---
# 说明：
#  - 不做最新日期判断；每次都全量抓取；
#  - 估值字段从 info 推导（PE、PS、PB）。
def fetch_comprehensive_financials(ticker: Union[str, yf.Ticker]) -> Optional[FinancialData]:
    """
    使用 yfinance 获取指定股票的完整财务报表和公司信息。
    包括年度/季度财务数据、资产负债表、现金流量表、收益和估值信息。
    接受股票代码字符串或预创建的 yfinance.Ticker 对象。
    """
    try:
        if isinstance(ticker, yf.Ticker):
            ticker_obj = ticker
            ticker_symbol = ticker_obj.ticker
        else:
            ticker_symbol = ticker
            ticker_obj = yf.Ticker(ticker_symbol)

        info = ticker_obj.info

        # 估值指标
        valuations: Dict[str, Optional[float]] = {}
        # 这里不对 info 进行 _deep_clean_json_serializable 处理，因为最终 model_dump 会被统一清理。
        price = info.get("regularMarketPrice")
        trailing_eps = info.get("trailingEps")
        trailing_pe = price / trailing_eps if price and trailing_eps else None
        price_to_sales = info.get("priceToSalesTrailing12Months")
        price_to_book = info.get("priceToBook")
        valuations = {
            "trailing_pe": trailing_pe,
            "price_to_sales": price_to_sales,
            "price_to_book": price_to_book,
        }

        financial_data_dict = {
            "ticker": ticker_symbol,
            "annual_financials": _convert_df_for_storage(ticker_obj.financials),
            "annual_balance_sheet": _convert_df_for_storage(ticker_obj.balance_sheet),
            "annual_cashflow": _convert_df_for_storage(ticker_obj.cashflow),
            "quarterly_financials": _convert_df_for_storage(ticker_obj.quarterly_financials),
            "quarterly_balance_sheet": _convert_df_for_storage(ticker_obj.quarterly_balance_sheet),
            "quarterly_cashflow": _convert_df_for_storage(ticker_obj.quarterly_cashflow),
            "annual_earnings": _convert_df_for_storage(ticker_obj.earnings),
            "quarterly_earnings": _convert_df_for_storage(ticker_obj.quarterly_earnings),
            "info": info, # 传入原始 info，最终保存时会统一清理
            "valuations": valuations,
            "fetched_at": datetime.utcnow(),
        }
        return FinancialData(**financial_data_dict)
    except Exception as exc:
        logger.error("Error fetching comprehensive financial data for %s: %s", ticker, exc)
        return None


def _prepare_data_for_interpretation(ticker_obj: yf.Ticker) -> Dict[str, Any]:
    """
    内部工具：获取并准备数据，专用于语义解读函数。
    此函数会转换为 orient='list' 格式，便于直接按指标名称访问列表。
    """
    data_for_interp: Dict[str, Any] = {
        "financials": {},  # Quarterly financials in orient='list'
        "earnings": {},    # Quarterly earnings in orient='list'
        "valuations": {},
        "info": {},
    }
    try:
        data_for_interp["financials"] = _convert_df_for_interpretation(ticker_obj.quarterly_financials)
    except Exception as exc:
        logger.warning("获取 %s 季度财务报表失败: %s", ticker_obj.ticker, exc)
    try:
        data_for_interp["earnings"] = _convert_df_for_interpretation(ticker_obj.quarterly_earnings)
    except Exception as exc:
        logger.warning("获取 %s 季度 earnings 数据失败: %s", ticker_obj.ticker, exc)
    try:
        info = ticker_obj.info
        # 这里仍需清理 info，因为解读和估值直接依赖它
        cleaned_info = _deep_clean_json_serializable(info)
        data_for_interp["info"] = cleaned_info
        
        price = cleaned_info.get("regularMarketPrice")
        trailing_eps = cleaned_info.get("trailingEps")
        trailing_pe = price / trailing_eps if price and trailing_eps else None
        price_to_sales = cleaned_info.get("priceToSalesTrailing12Months")
        price_to_book = cleaned_info.get("priceToBook")
        data_for_interp["valuations"] = {
            "trailing_pe": trailing_pe,
            "price_to_sales": price_to_sales,
            "price_to_book": price_to_book,
        }
    except Exception as exc:
        logger.warning("获取 %s 公司信息失败: %s", ticker_obj.ticker, exc)
    return data_for_interp



# 【修改点：_interpret_financials 函数】
# 改动原因：将输出的解读文本和指标名称改为英文，并使用 _format_value 简化数字显示。
def _interpret_financials(data: Dict[str, Any]) -> List[str]:
    """
    对财报数据进行语义解读，生成英文摘要。
    期望 data['financials'] 和 data['earnings'] 是 `orient='list'` 格式。
    此函数仅生成基于季度数据比较的解读，不再包含估值指标的解读。
    """
    interpretations: List[str] = []
    financials_data = data.get("financials", {})
    earnings_data = data.get("earnings", {})

    metrics_to_interpret = {
        "Total Revenue": "Total Revenue",
        "Net Income": "Net Income",
        "Operating Income": "Operating Income",
        "Cost Of Revenue": "Cost Of Revenue",
        "Gross Profit": "Gross Profit",
    }

    for metric_name, english_name in metrics_to_interpret.items():
        values = financials_data.get(metric_name)
        if values and len(values) >= 2:
            latest = values[0]
            previous = values[1]
            if latest is not None and previous is not None:
                diff = latest - previous
                pct = (diff / previous * 100) if previous != 0 else None
                trend = "increased" if diff > 0 else ("decreased" if diff < 0 else "remained flat")
                if pct is not None:
                    interpretations.append(
                        f"Quarterly {english_name} {trend} by {_format_value(abs(diff))} ({pct:+.1f}%) from {_format_value(previous)} to {_format_value(latest)}."
                    )

    revs = earnings_data.get("Revenue")
    epss = earnings_data.get("Earnings")

    if revs and len(revs) >= 2:
        rev_latest = revs[0]
        rev_previous = revs[1]
        if rev_latest is not None and rev_previous is not None:
            rev_diff = rev_latest - rev_previous
            rev_pct = (rev_diff / rev_previous * 100) if rev_previous != 0 else None
            trend = "increased" if rev_diff > 0 else ("decreased" if rev_diff < 0 else "remained flat")
            if rev_pct is not None:
                interpretations.append(
                    f"Quarterly Revenue {trend} by {_format_value(abs(rev_diff))} ({rev_pct:+.1f}%) from {_format_value(rev_previous)} to {_format_value(rev_latest)}."
                )

    if epss and len(epss) >= 2:
        eps_latest = epss[0]
        eps_previous = epss[1]
        if eps_latest is not None and eps_previous is not None:
            eps_diff = eps_latest - eps_previous
            eps_pct = (eps_diff / eps_previous * 100) if eps_previous != 0 else None
            trend = "increased" if eps_diff > 0 else ("decreased" if eps_diff < 0 else "remained flat")
            if eps_pct is not None:
                interpretations.append(
                    f"Quarterly EPS {trend} by {_format_value(abs(eps_diff))} ({eps_pct:+.1f}%) from {_format_value(eps_previous)} to {_format_value(eps_latest)}."
                )

    if not interpretations:
        interpretations.append("Insufficient financial data for interpretation.")
    return interpretations

# --- 合并：按 `index`（日期）去重，新的覆盖旧的，结果按日期倒序 ---
def _merge_by_index(old: List[Dict], new: List[Dict]) -> List[Dict]:
    """
    按 'index' 字段（通常为日期字符串）去重合并两个记录列表。
    新数据会覆盖旧数据中相同 'index' 的记录。
    合并后的结果会按 'index' 倒序（最新日期在前）。
    """
    by_idx: Dict[str, Dict] = {}
    for rec in old:
        # 注意：这里的 index 已经是 date 字段 (因为 _convert_df_for_storage 的改动)
        idx = str(rec.get("date")) 
        by_idx[idx] = rec
    for rec in new:
        idx = str(rec.get("date")) # 注意：这里的 index 已经是 date 字段
        by_idx[idx] = rec  # 覆盖同日期
    # 倒序：按 index 字符串排序后逆序（index 多为日期字符串）
    try:
        # 排序键改为 'date' 字段
        merged = sorted(by_idx.values(), key=lambda r: str(r.get("date")), reverse=True)
    except Exception:
        # 如果索引不是日期格式，或者排序失败，则直接返回无序列表
        merged = list(by_idx.values())
    return merged


# --- 合并：旧/新 JSON 的规则（季度/年度表做去重合并，其它直接覆盖） ---
def _merge_financial_json(old_obj: Dict[str, Any], new_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并旧的财务数据 JSON 对象与新的财务数据 JSON 对象。
    对于年度和季度财报、资产负债表、现金流量表和收益数据，会进行去重合并，新数据覆盖旧数据。
    对于其他字段（如 info, valuations），新值将直接覆盖旧值。
    """
    res = old_obj.copy() if old_obj else {}
    # 直接用新数据覆盖非报表列表字段
    res.update({k: v for k, v in new_obj.items() if k not in {
        "annual_financials", "annual_balance_sheet", "annual_cashflow",
        "quarterly_financials", "quarterly_balance_sheet", "quarterly_cashflow",
        "annual_earnings", "quarterly_earnings"
    }})
    # 对所有财务报表列表进行按索引合并
    for key in [
        "annual_financials", "annual_balance_sheet", "annual_cashflow",
        "quarterly_financials", "quarterly_balance_sheet", "quarterly_cashflow",
        "annual_earnings", "quarterly_earnings"
    ]:
        res[key] = _merge_by_index(old_obj.get(key, []), new_obj.get(key, []))
    return res


# --- 持久化：保存/合并财报 JSON 到 GCS（如有旧文件则合并） ---
# 返回：gs://bucket/XXX_financials.json
def save_financials_to_file(ticker: str, data: FinancialData, cache_meta: Optional[Dict[str, Any]] = None) -> str:
    """
    将完整财务报表保存为 JSON 文件并返回保存路径。
    将文件上传到 Google Cloud Storage。新数据会与现有文件合并，以实现无条件增量更新。
    如果GCS上存在旧文件，会下载并与新数据进行按 `index` 去重合并，然后上传合并后的数据。
    """
    # 1. 首先将 Pydantic 模型转换为 Python 字典
    raw_output_dict = data.model_dump()
    # 2. 对整个字典进行深度清理，确保所有键和值都是 JSON 可序列化类型
    new_serializable_dict = _deep_clean_json_serializable(raw_output_dict)
    if cache_meta and isinstance(cache_meta, dict):
        new_serializable_dict["cache_meta"] = _deep_clean_json_serializable(cache_meta)

    blob_name = _get_financial_blob_name(ticker)

    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(blob_name)

            merged_dict = new_serializable_dict
            # 如果现有文件存在，则先下载并合并
            if blob.exists():
                try:
                    old_text = blob.download_as_text()
                    old_obj = json.loads(old_text)
                    merged_dict = _merge_financial_json(old_obj, new_serializable_dict)
                except Exception as merge_exc:
                    logger.warning("Merging existing financial data for %s failed: %s. Overwriting with new data.", ticker, merge_exc)
                    merged_dict = new_serializable_dict # 确保即使合并失败也用新数据覆盖

            # 转换为 JSON 字符串
            json_content = json.dumps(merged_dict, indent=2, ensure_ascii=False)
            # 上传到 GCS
            blob.cache_control = "public, max-age=300, stale-while-revalidate=86400"
            blob.upload_from_string(json_content, content_type="application/json")
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("Failed to save financial data for %s to GCS: %s", ticker, exc)

    # 本地 fallback（本地开发或 GCS 异常时）
    local_path = _get_local_financial_filepath(ticker)
    merged_local = new_serializable_dict
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                old_local = json.load(f)
            if isinstance(old_local, dict):
                merged_local = _merge_financial_json(old_local, new_serializable_dict)
        except Exception as exc:
            logger.warning("Failed to merge local financial cache for %s: %s", ticker, exc)
            merged_local = new_serializable_dict

    try:
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(merged_local, f, ensure_ascii=False, indent=2)
        return local_path
    except Exception as exc:
        logger.error("Failed to save financial data for %s to local fallback: %s", ticker, exc)
        return ""  # 返回空字符串表示失败


# 【新增辅助函数 1：_format_value】
# 改动原因：统一格式化数字，包括大数字的 K/M/B 缩写和 None 值处理。
def _format_value(value: Any, is_percentage: bool = False, decimals: int = 2) -> str:
    """格式化数值，大数字加逗号，None转N/A，百分比加%"""
    if value is None:
        return "N/A"
    try:
        # 尝试将值转换为浮点数，便于格式化
        float_value = float(value)
        if is_percentage:
            return f"{float_value:,.{decimals}f}%"
        # 对于很大的数字，可以考虑使用简写 K/M/B
        if abs(float_value) >= 1_000_000_000:
            return f"{float_value / 1_000_000_000:,.{decimals}f}B"
        elif abs(float_value) >= 1_000_000:
            return f"{float_value / 1_000_000:,.{decimals}f}M"
        elif abs(float_value) >= 1_000:
            return f"{float_value / 1_000:,.{decimals}f}K"
        return f"{float_value:,.{decimals}f}"
    except (ValueError, TypeError):
        return str(value) # Fallback for non-numeric or complex types

# 【新增辅助函数 2：_get_latest_n_records】
# 改动原因：方便从列表中获取最新的 N 条记录。
def _get_latest_n_records(data_list: List[Dict], n: int) -> List[Dict]:
    """从按日期降序排列的记录列表中获取前N条记录。"""
    return data_list[:n]

# 【新增辅助函数 3：_extract_and_format_metrics】
# 改动原因：封装从单个财报记录中提取和格式化指定指标的逻辑。
def _extract_and_format_metrics(record: Dict, metric_keys: Dict[str, str]) -> List[str]:
    """从单条财报记录中提取并格式化指定的指标。"""
    lines = []
    for key, display_name in metric_keys.items():
        value = record.get(key)
        lines.append(f"- {display_name}: {_format_value(value)}")
    return lines

# 【修改点：_generate_ai_context_text 函数】
# 改动原因：将所有输出文本标签改为英文，并使用 _format_value 简化数字显示。
def _generate_ai_context_text(comprehensive_data: FinancialData, interpretations: List[str]) -> str:
    """
    根据完整的财务数据和解读生成用于 AI 的简洁上下文文本（全英文）。
    该文本包含公司基本信息、估值、近期原始财务数据概览和关键财务指标的解读。
    """
    lines: List[str] = []

    ticker = comprehensive_data.ticker
    info = comprehensive_data.info
    valuations = comprehensive_data.valuations

    # 1. Basic Info and Date
    lines.append(f"Ticker: {ticker.upper()}")
    lines.append(f"Date: {_today_str()}")
    company_name = info.get("shortName") or info.get("longName")
    if company_name:
        lines.append(f"Company Name: {company_name}")
    industry = info.get("industry")
    if industry:
        lines.append(f"Industry: {industry}")
    
    lines.append("\n--- Valuation Metrics ---")
    pe = valuations.get("trailing_pe")
    if pe is not None:
        lines.append(f"Price-to-Earnings (PE): {_format_value(pe, decimals=2)}")
    ps = valuations.get("price_to_sales")
    if ps is not None:
        lines.append(f"Price-to-Sales (P/S): {_format_value(ps, decimals=2)}")
    pb = valuations.get("price_to_book")
    if pb is not None:
        lines.append(f"Price-to-Book (P/B): {_format_value(pb, decimals=2)}")

    # 2. Recent Financials Overview (Raw Data)
    lines.append("\n--- Recent Financials Overview ---")

    # Define key metrics to extract (in English)
    income_statement_keys = {
        "Total Revenue": "Total Revenue",
        "Gross Profit": "Gross Profit",
        "Operating Income": "Operating Income",
        "Net Income": "Net Income",
        "EBITDA": "EBITDA",
        "Diluted EPS": "Diluted EPS"
    }
    balance_sheet_keys = {
        "Total Assets": "Total Assets",
        "Current Assets": "Current Assets",
        "Total Liabilities Net Minority Interest": "Total Liabilities",
        "Total Debt": "Total Debt",
        "Common Stock Equity": "Common Stock Equity",
        "Cash And Cash Equivalents": "Cash & Cash Equivalents"
    }
    cash_flow_keys = {
        "Operating Cash Flow": "Operating Cash Flow",
        "Investing Cash Flow": "Investing Cash Flow",
        "Financing Cash Flow": "Financing Cash Flow",
        "Free Cash Flow": "Free Cash Flow",
        "Capital Expenditure": "Capital Expenditure"
    }

    # Latest Annual Report
    latest_annual_financial_records = _get_latest_n_records(comprehensive_data.annual_financials, 1)
    latest_annual_balance_records = _get_latest_n_records(comprehensive_data.annual_balance_sheet, 1)
    latest_annual_cashflow_records = _get_latest_n_records(comprehensive_data.annual_cashflow, 1)

    if latest_annual_financial_records:
        annual_date = latest_annual_financial_records[0].get("date", "N/A")
        lines.append(f"\nLatest Annual Report (as of {annual_date}):")
        lines.append("  Income Statement:")
        lines.extend("    " + s for s in _extract_and_format_metrics(latest_annual_financial_records[0], income_statement_keys))
        if latest_annual_balance_records:
            lines.append("  Balance Sheet:")
            lines.extend("    " + s for s in _extract_and_format_metrics(latest_annual_balance_records[0], balance_sheet_keys))
        if latest_annual_cashflow_records:
            lines.append("  Cash Flow Statement:")
            lines.extend("    " + s for s in _extract_and_format_metrics(latest_annual_cashflow_records[0], cash_flow_keys))
    else:
        lines.append("\nNo recent annual report data available.")

    # Latest two Quarterly Reports (for comparison)
    latest_quarterly_financial_records = _get_latest_n_records(comprehensive_data.quarterly_financials, 2)
    latest_quarterly_balance_records = _get_latest_n_records(comprehensive_data.quarterly_balance_sheet, 2)
    latest_quarterly_cashflow_records = _get_latest_n_records(comprehensive_data.quarterly_cashflow, 2)

    if latest_quarterly_financial_records:
        q1_record = latest_quarterly_financial_records[0]
        q1_date = q1_record.get("date", "N/A")
        lines.append(f"\nLatest Quarterly Report (as of {q1_date}):")
        lines.append("  Income Statement:")
        lines.extend("    " + s for s in _extract_and_format_metrics(q1_record, income_statement_keys))
        if latest_quarterly_balance_records:
            lines.append("  Balance Sheet:")
            lines.extend("    " + s for s in _extract_and_format_metrics(latest_quarterly_balance_records[0], balance_sheet_keys))
        if latest_quarterly_cashflow_records:
            lines.append("  Cash Flow Statement:")
            lines.extend("    " + s for s in _extract_and_format_metrics(latest_quarterly_cashflow_records[0], cash_flow_keys))
        
        if len(latest_quarterly_financial_records) > 1:
            q2_record = latest_quarterly_financial_records[1]
            q2_date = q2_record.get("date", "N/A")
            lines.append(f"\nPrevious Quarterly Report (as of {q2_date}):")
            lines.append("  Income Statement:")
            lines.extend("    " + s for s in _extract_and_format_metrics(q2_record, income_statement_keys))
            if len(latest_quarterly_balance_records) > 1:
                lines.append("  Balance Sheet:")
                lines.extend("    " + s for s in _extract_and_format_metrics(latest_quarterly_balance_records[1], balance_sheet_keys))
            if len(latest_quarterly_cashflow_records) > 1:
                lines.append("  Cash Flow Statement:")
                lines.extend("    " + s for s in _extract_and_format_metrics(latest_quarterly_cashflow_records[1], cash_flow_keys))
    else:
        lines.append("\nNo recent quarterly report data available.")

    # 3. Financial Highlights & Interpretation
    if interpretations:
        lines.append("\n--- Financial Highlights & Interpretation ---")
        lines.extend(f"- {interp}" for interp in interpretations)
    else:
        lines.append("\n--- Financial Highlights & Interpretation ---")
        lines.append("No specific interpretations available, possibly due to incomplete or non-comparable data.")

    return "\n".join(lines) + "\n"

# --- 持久化：保存 AI context 文本到 GCS（ai_context/{T}/{YYYY-MM-DD}.txt） ---
def save_ai_context_to_file(ticker: str, text: str, date_str: Optional[str] = None) -> str:
    """
    将生成的 AI 上下文文本保存到 GCS 中的专用路径下，并返回其 GCS 路径。
    路径格式为 `ai_context/{TICKER}/{YYYY-MM-DD}.txt`。
    """
    date_str = date_str or _today_str()
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob_name = f"ai_context/{ticker.upper()}/{date_str}.txt"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(text, content_type="text/plain")
        return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
    except Exception as exc:
        logger.error("Failed to save AI context for %s on %s: %s", ticker, date_str, exc)
        return ""


# --- 索引：维护当日清单 ai_context/daily_index/{YYYY-MM-DD}.json ---
def _append_daily_index(ticker: str, gs_path: str, date_str: Optional[str] = None) -> str:
    """
    维护每日 AI context 文件的索引清单。
    清单文件位于 `ai_context/daily_index/{YYYY-MM-DD}.json`，包含当日处理过的股票及其 AI context GCS 路径。
    如果清单中已存在该股票，则不重复添加。
    """
    date_str = date_str or _today_str()
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        idx_blob = bucket.blob(f"ai_context/daily_index/{date_str}.json")

        records: List[Dict[str, str]] = []
        if idx_blob.exists():
            try:
                records = json.loads(idx_blob.download_as_text())
            except Exception as read_exc:
                logger.warning("Failed to read existing daily index for %s: %s. Starting fresh.", date_str, read_exc)
                records = []
        
        # 检查是否已存在该 ticker 的记录
        if not any(r.get("ticker") == ticker.upper() for r in records):
            records.append({"ticker": ticker.upper(), "path": gs_path})
        
        idx_blob.upload_from_string(json.dumps(records, ensure_ascii=False, indent=2), content_type="application/json")
        return f"gs://{GCS_BUCKET_NAME}/ai_context/daily_index/{date_str}.json"
    except Exception as exc:
        logger.error("Failed to update daily index for %s: %s", date_str, exc)
        return ""


# --- 工作流：抓取 -> 合并保存 -> 生成 context -> 更新缓存/清单 ---
# --- 入口：单支股票的完整处理流程 ---
# 1) yfinance 抓取
# 2) 生成中文要点
# 3) 合并并写回 JSON（可选）
# 4) 生成 & 写入 AI context + 更新当日清单
# 5) 更新内存缓存 earnings_cache
def process_and_cache_ticker(
    ticker: str,
    should_save_file: bool = False,
    next_earnings_date: Optional[date] = None,
    refresh_reason: str = "",
) -> Dict[str, Any]:
    """
    获取指定股票的完整财务数据，进行语义解读，更新缓存，并可选地保存到文件。
    此函数会复用同一个 yfinance.Ticker 对象，避免重复请求。
    如果 should_save_file 为 True，将在 GCS 中保存合并后的完整财报并生成 AI 上下文。
    """
    logger.info("Processing data for ticker: %s", ticker)
    # Create Ticker object once and reuse it
    ticker_obj = yf.Ticker(ticker)

    # 1. Fetch comprehensive raw data for storage/full access (reuse ticker_obj)
    comprehensive_data = fetch_comprehensive_financials(ticker_obj)
    if comprehensive_data is None:
        logger.error("Failed to fetch comprehensive data for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Failed to fetch comprehensive data for {ticker}")

    # 2. Prepare data specifically for interpretation
    data_for_interpretation = _prepare_data_for_interpretation(ticker_obj)
    # 3. Perform semantic interpretation
    interpretations = _interpret_financials(data_for_interpretation)

    saved_file_path: Optional[str] = None
    ai_context_path: Optional[str] = None
    daily_idx_path: Optional[str] = None

    cache_meta = {
        "last_refreshed_at": datetime.now(tz).isoformat(),
        "next_earnings_date": next_earnings_date.isoformat() if isinstance(next_earnings_date, date) else None,
        "refresh_reason": refresh_reason or "manual_or_batch",
        "schedule_source": "trading_data_engine" if TRADING_DATA_ENGINE_URL else "unknown",
    }
    fundamental_signal: Dict[str, Any] = {}
    try:
        financial_obj_for_factor = _deep_clean_json_serializable(comprehensive_data.model_dump())
        if isinstance(financial_obj_for_factor, dict):
            financial_obj_for_factor["cache_meta"] = cache_meta
            fundamental_signal = _compute_fundamental_factor_signal(financial_obj_for_factor)
    except Exception as exc:
        logger.warning("Failed to compute fundamental signal for %s: %s", ticker, exc)
        fundamental_signal = {}

    if should_save_file:
        # 保存完整财报到 GCS（自动合并历史）
        saved_file_path = save_financials_to_file(ticker, comprehensive_data, cache_meta=cache_meta)
        if not saved_file_path:
            logger.warning("Could not save file for %s, interpretation will still be cached.", ticker)

        # 生成并保存 AI 上下文
        try:
            # 传递 comprehensive_data 而不是 data_for_interpretation
            ai_context_text = _generate_ai_context_text(comprehensive_data, interpretations)
            ai_context_path = save_ai_context_to_file(ticker, ai_context_text)
            if ai_context_path:
                daily_idx_path = _append_daily_index(ticker, ai_context_path)
        except Exception as ctx_exc:
            logger.warning("Failed to generate or save AI context for %s: %s", ticker, ctx_exc)

    # Update cache with timezone 'America/Los_Angeles'
    earnings_cache[ticker] = {
        "interpretation_data": data_for_interpretation,  # Cache data used for interpretation
        "interpretations": interpretations,
        "last_updated": datetime.now(tz).isoformat(),
        "saved_file_path": saved_file_path,
        "ai_context_path": ai_context_path,
        "daily_index_path": daily_idx_path,
        "cache_meta": cache_meta,
        "fundamental_signal": fundamental_signal,
        "stockflow_input": fundamental_signal.get("stockflow_input") if isinstance(fundamental_signal, dict) else None,
    }
    logger.info("Successfully processed and cached data for %s", ticker)
    return earnings_cache[ticker]


def _get_or_refresh_interpreted_earnings(ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
    t = ticker.upper()

    if not force_refresh:
        l1 = _get_financial_from_l1(t)
        if isinstance(l1, dict):
            payload = _build_interpreted_payload_from_financial_obj(t, l1, cache_layer="l1")
            earnings_cache[t] = payload
            return payload

    with _FINANCIAL_LOCK:
        wait_ev = _FINANCIAL_INFLIGHT.get(t)
        if wait_ev is None:
            wait_ev = Event()
            _FINANCIAL_INFLIGHT[t] = wait_ev
            leader = True
        else:
            leader = False

    if not leader:
        wait_ev.wait(timeout=15.0)
        l1_after_wait = _get_financial_from_l1(t)
        if isinstance(l1_after_wait, dict):
            payload = _build_interpreted_payload_from_financial_obj(t, l1_after_wait, cache_layer="l1-after-wait")
            earnings_cache[t] = payload
            return payload
        l2_after_wait = _load_financials_from_storage(t)
        if isinstance(l2_after_wait, dict):
            _set_financial_to_l1(t, l2_after_wait)
            payload = _build_interpreted_payload_from_financial_obj(t, l2_after_wait, cache_layer="l2-after-wait")
            earnings_cache[t] = payload
            return payload
        raise HTTPException(status_code=502, detail=f"No financial data available for {t} after wait.")

    try:
        l2_obj = _load_financials_from_storage(t)
        next_earnings = _fetch_next_earnings_date_from_trading_service(t)
        need_refresh, reason = _should_refresh_financials(
            cached_payload=l2_obj if isinstance(l2_obj, dict) else None,
            next_earnings_date=next_earnings,
            force_refresh=force_refresh,
        )

        if not need_refresh and isinstance(l2_obj, dict):
            _set_financial_to_l1(t, l2_obj)
            payload = _build_interpreted_payload_from_financial_obj(t, l2_obj, cache_layer="l2")
            earnings_cache[t] = payload
            return payload

        process_and_cache_ticker(
            t,
            should_save_file=True,
            next_earnings_date=next_earnings,
            refresh_reason=reason,
        )
        refreshed = _load_financials_from_storage(t)
        if isinstance(refreshed, dict):
            _set_financial_to_l1(t, refreshed)
            payload = _build_interpreted_payload_from_financial_obj(t, refreshed, cache_layer="upstream")
            earnings_cache[t] = payload
            return payload

        # Worst case fallback: return in-memory interpreted payload from this process.
        mem_payload = earnings_cache.get(t)
        if isinstance(mem_payload, dict):
            mem_payload = dict(mem_payload)
            mem_payload["cacheLayer"] = "upstream-memory"
            earnings_cache[t] = mem_payload
            return mem_payload
        raise HTTPException(status_code=502, detail=f"Refreshed {t} but failed to load persisted financial payload.")
    except HTTPException:
        raise
    except Exception as exc:
        stale_obj = _load_financials_from_storage(t)
        if isinstance(stale_obj, dict):
            _set_financial_to_l1(t, stale_obj)
            payload = _build_interpreted_payload_from_financial_obj(
                t,
                stale_obj,
                cache_layer="l2-stale",
                stale=True,
                stale_reason=str(exc),
            )
            earnings_cache[t] = payload
            return payload
        raise HTTPException(status_code=502, detail=f"Failed to get earnings for {t}: {exc}")
    finally:
        with _FINANCIAL_LOCK:
            done = _FINANCIAL_INFLIGHT.pop(t, None)
        if done is not None:
            done.set()


def _get_or_refresh_financial_object(ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
    t = ticker.upper()
    # Reuse the same refresh orchestration path.
    _ = _get_or_refresh_interpreted_earnings(t, force_refresh=force_refresh)

    l1 = _get_financial_from_l1(t)
    if isinstance(l1, dict):
        return l1
    l2 = _load_financials_from_storage(t)
    if isinstance(l2, dict):
        _set_financial_to_l1(t, l2)
        return l2

    # Last fallback: direct upstream fetch (without persistence metadata).
    data = fetch_comprehensive_financials(t)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Financial data for {t} not found or could not be fetched.")
    obj = _deep_clean_json_serializable(data.model_dump())
    if not isinstance(obj, dict):
        raise HTTPException(status_code=500, detail=f"Failed to build financial payload for {t}.")
    return obj


# --- 批量任务：遍历 tickers，逐只执行工作流（失败不中断批次） ---
def update_earnings_cache_job(tickers: List[str]) -> None:
    """
    批量更新指定股票的财报缓存的调度任务。
    遍历股票列表，对每只股票执行数据抓取、解读和保存操作。
    单个股票处理失败不会中断整个批次，错误会被记录。
    """
    logger.info("Starting scheduled update for %d tickers...", len(tickers))
    for ticker in tickers:
        try:
            payload = _get_or_refresh_interpreted_earnings(ticker, force_refresh=False)
            logger.info("Processed %s via %s", ticker, payload.get("cacheLayer", "unknown"))
        except Exception as exc:
            logger.error("Error during scheduled processing for %s: %s", ticker, exc)
    logger.info("Scheduled earnings update finished.")


# -----------------------------
# 调度器设置
# -----------------------------
scheduler: Optional[BackgroundScheduler] = None
default_tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "AMD", "JPM", "V", "BRK-B", "WMT", "COST", "KO", "NKE", "LLY", "UNH", "CAT", "DIS", "NFLX"] # 默认关注的股票列表

@app.on_event("startup")
# --- FastAPI 生命周期：启动时先跑一轮默认列表，并启动定时任务 ---
async def startup_event():
    """
    应用启动事件处理函数。
    在应用启动时，会立即为默认股票列表抓取并处理数据，并初始化并启动后台调度器，
    以便定期执行财报数据更新任务。
    """
    global scheduler
    logger.info("Application startup event triggered.")
    
    # 移除或注释掉启动时立即处理默认股票的代码块，避免多实例启动时的竞态条件和数据不一致问题
    # 首次启动时立即处理一次默认股票
    # logger.info("Initial data processing for default tickers...")
    # for ticker in default_tickers:
    #     try:
    #         process_and_cache_ticker(ticker, should_save_file=True) 
    #     except Exception as exc:
    #         logger.error("Initial processing failed for %s: %s", ticker, exc)

    # 初始化并启动调度器
    # scheduler = BackgroundScheduler(timezone=tz)
    # # 使用 IntervalTrigger 每 7 天执行一次
    # trigger = IntervalTrigger(days=7)
    # scheduler.add_job(update_earnings_cache_job, trigger, args=[default_tickers])
    # scheduler.start()
    # logger.info("Scheduler started successfully. Financial data will update every 7 days.")

    logger.info("Application startup complete. Data updates will be handled by explicit API calls or scheduled jobs (if enabled).")

@app.on_event("shutdown")
async def shutdown_event():
    """
    应用关闭事件处理函数。
    在应用关闭时，会安全地关闭后台调度器。
    """
    global scheduler
    if scheduler:
        scheduler.shutdown()
        logger.info("Scheduler shutdown.")
    global _report_source_service
    if _report_source_service is not None:
        try:
            _report_source_service.fetcher.close()
        except Exception:
            pass
    logger.info("Application shutdown event triggered.")


# -----------------------------
# API 路由（供 QA / 卡片 / 运维 使用）
# -----------------------------

# =========================
#         API 路由
# =========================


@app.get("/financial/{ticker}", summary="获取指定股票的全部原始财务报表", response_model=FinancialData)
async def get_raw_financial_data(
    ticker: str,
    force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新上游"),
) -> FinancialData:
    """
    获取指定股票的全部原始财务报表数据（年度、季度、收益、资产负债、现金流等）。

    **主要行为：**
    *   **获取数据：** ✅ 会实时从 `yfinance` 获取指定股票的年度和季度财务数据、资产负债表、现金流量表、收益数据和公司信息。
    *   **生成 AI Context：** ❌ 不会。
    *   **保存 JSON：** ❌ 不会，仅将数据作为 API 响应返回。

    **用途：**
    适用于需要获取指定股票最新、最原始的完整财报数据的场景，不涉及数据持久化或 AI Context 生成。
    """
    ticker = ticker.upper()
    payload = _get_or_refresh_financial_object(ticker, force_refresh=bool(force_refresh))
    try:
        return FinancialData(**payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse financial payload for {ticker}: {exc}")


@app.post("/save/{ticker}", summary="获取并保存指定股票的全部原始财务报表到GCS")
async def save_raw_financial_data(ticker: str) -> Dict[str, str]:
    """
    获取指定股票的全部原始财务报表数据并保存到 Google Cloud Storage (GCS) 中。

    **主要行为：**
    *   **获取数据：** ✅ 会实时从 `yfinance` 获取指定股票的完整财务数据。
    *   **生成 AI Context：** ❌ 不会。
    *   **保存 JSON：** ✅ 会将获取到的完整财报数据以 JSON 格式保存到 GCS。如果 GCS 上已存在同名文件，会进行增量合并（新数据覆盖旧数据，并保持最新日期在前）。

    **用途：**
    适用于仅需手动触发数据抓取和存储，但不需要立即生成 AI Context 或解读的场景。
    """
    ticker = ticker.upper()
    data = fetch_comprehensive_financials(ticker)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Financial data for {ticker} not found or could not be fetched.")
    filepath = save_financials_to_file(ticker, data)
    if not filepath:
        raise HTTPException(status_code=500, detail=f"Failed to save financial data for {ticker}.")
    return {"ticker": ticker, "file_path": filepath}


@app.get("/earnings/{ticker}", summary="获取指定股票的财报解读及用于解读的原始数据")
async def get_interpreted_earnings(
    ticker: str,
    force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新上游"),
) -> Dict:
    """
    获取指定股票的财报解读结果和用于生成解读的季度财务数据。
    数据从内部缓存中获取。如果缓存中不存在该股票的数据，将返回 404 错误。
    建议在请求前先调用 `/refresh/{ticker}` 或等待定时任务更新。

    **主要行为：**
    *   **获取数据：** ❌ 不会，而是从内存缓存 (`earnings_cache`) 中检索数据。
    *   **生成 AI Context：** ❌ 不会，而是返回缓存中预先生成的解读文本和用于解读的原始季度数据。
    *   **保存 JSON：** ❌ 不会。

    **用途：**
    适用于快速获取已处理并缓存的财报解读内容及相关季度数据，无需再次调用外部 API。
    """
    ticker = ticker.upper()
    return _get_or_refresh_interpreted_earnings(ticker, force_refresh=bool(force_refresh))


@app.get("/stockflow/fundamental/{ticker}", summary="获取供 StockFlow 融合的财报因子信号")
async def get_stockflow_fundamental_signal(
    ticker: str,
    force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新上游"),
) -> Dict[str, Any]:
    t = ticker.upper()
    payload = _get_or_refresh_interpreted_earnings(t, force_refresh=bool(force_refresh))
    fundamental_signal = payload.get("fundamental_signal")
    if not isinstance(fundamental_signal, dict):
        raise HTTPException(status_code=502, detail=f"Failed to compute fundamental signal for {t}.")
    return {
        "ticker": t,
        "cacheLayer": payload.get("cacheLayer"),
        "last_updated": payload.get("last_updated"),
        "stockflow_input": payload.get("stockflow_input"),
        "fundamental_signal": fundamental_signal,
    }


@app.post("/refresh/{ticker}", summary="手动刷新并保存指定股票的财报数据及解读")
async def refresh_and_save_earnings(ticker: str) -> Dict[str, Any]:
    """
    手动触发对指定股票的财报数据进行刷新。
    此操作会立即从 yfinance 抓取最新数据，执行语义解读，更新内部缓存，
    并将完整的原始数据保存到 Google Cloud Storage。
    返回更新后的缓存信息，包括解读结果和保存文件的 GCS 路径。

    **主要行为：**
    *   **获取数据：** ✅ 会实时从 `yfinance` 获取指定股票的完整财务数据。
    *   **生成 AI Context：** ✅ 会根据最新的财报数据和公司信息生成 AI Context 文本，包含关键指标的语义解读。
    *   **保存 JSON：** ✅ 会将完整的原始财报数据保存到 GCS (与 `/save/{ticker}` 行为一致)。同时，也会将生成的 AI Context 文本保存到 GCS 的 `ai_context/{TICKER}/{YYYY-MM-DD}.txt` 路径下，并更新当日的 `daily_index.json` 清单。

    **用途：**
    这是最全面的单只股票刷新接口，适用于手动触发某只股票的数据更新、解读和持久化。
    """
    ticker = ticker.upper()
    try:
        next_earnings = _fetch_next_earnings_date_from_trading_service(ticker)
        result = process_and_cache_ticker(
            ticker,
            should_save_file=True,
            next_earnings_date=next_earnings,
            refresh_reason="force_refresh_api",
        )
        refreshed_obj = _load_financials_from_storage(ticker)
        if isinstance(refreshed_obj, dict):
            _set_financial_to_l1(ticker, refreshed_obj)
        return {"message": f"Successfully refreshed and saved data for {ticker}.", "data": result}
    except HTTPException as e:
        raise e
    except Exception as exc:
        logger.error("Failed to manually refresh data for %s: %s", ticker, exc)
        raise HTTPException(status_code=500, detail=f"Failed to refresh data for {ticker}: {exc}")


@app.post("/batch_process_all", summary="手动触发所有默认股票的批量财报更新")
# --- API：手动触发默认列表的批量刷新 ---
async def manual_batch_process_all() -> Dict[str, Any]:
    """
    手动触发对所有默认关注股票（定义在 `default_tickers` 中）的批量财报更新任务。
    此操作将为每支股票抓取最新数据、进行解读并保存到 GCS 中。
    这是一个后台任务，立即返回，请查看服务日志以获取详细进度。
    
    **主要行为：**
    *   **获取数据：** ✅ 会遍历内部预设的 `default_tickers` 列表中的所有股票，逐一从 `yfinance` 获取完整数据。
    *   **生成 AI Context：** ✅ 会为 `default_tickers` 列表中的每只股票生成 AI Context 文本。
    *   **保存 JSON：** ✅ 会为 `default_tickers` 列表中的每只股票将原始财报 JSON 和 AI Context 文本保存到 GCS，并更新当日的 `daily_index.json` 清单。

    **用途：**
    适用于运维或开发者手动触发对核心关注股票的全量更新，无需指定股票列表。
    这是一个后台任务，立即返回，请查看服务日志以获取详细进度。
    """
    logger.info("Manual batch processing triggered for all default tickers.")
    update_earnings_cache_job(default_tickers)
    return {"message": "Batch processing initiated for default tickers. Check logs for details."}


class TickerList(BaseModel):
    tickers: List[str] = Field(default_factory=list, description="要刷新的股票列表")


class ReportSourceBatchRequest(BaseModel):
    tickers: List[str] = Field(default_factory=list, description="要获取财报官网来源的股票列表")
    force_refresh: bool = Field(default=False, description="是否绕过缓存强制刷新")


@app.post("/batch_refresh", summary="批量刷新指定股票列表")
# --- API：批量刷新自定义列表（图卡/运营调度用） ---
async def batch_refresh(payload: TickerList) -> Dict[str, Any]:
    """
    批量刷新指定股票列表的财报数据。
    如果请求体中提供了股票列表，则刷新这些股票；否则，刷新默认股票列表。
    此操作是一个后台任务，立即返回，请查看服务日志以获取详细进度。

    **主要行为：**
    *   **获取数据：** ✅ 会根据请求体中提供的股票列表（如果为空则使用 `default_tickers`）中的所有股票，逐一从 `yfinance` 获取完整数据。
    *   **生成 AI Context：** ✅ 会为请求列表中的每只股票生成 AI Context 文本。
    *   **保存 JSON：** ✅ 会为请求列表中的每只股票将原始财报 JSON 和 AI Context 文本保存到 GCS，并更新当日的 `daily_index.json` 清单。

    **用途：**
    **这是图卡系统每天定时调度时推荐使用的接口。** 它提供了灵活性，允许图卡系统根据自身需要动态地指定要刷新的股票列表，而不是固定刷新所有默认股票。
    此操作是一个后台任务，立即返回，请查看服务日志以获取详细进度。
    """
    tickers_to_refresh = [t.upper() for t in (payload.tickers or [])]
    if not tickers_to_refresh:
        tickers_to_refresh = default_tickers
    logger.info("Batch refresh requested for tickers: %s", tickers_to_refresh)
    update_earnings_cache_job(tickers_to_refresh)
    return {"message": f"Batch processing initiated for {len(tickers_to_refresh)} tickers. Check logs for details."}


@app.get("/stockflow/report_source/{ticker}", summary="获取单只股票的财报官网来源并做验证")
async def get_report_source(
    ticker: str,
    force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新"),
) -> Dict[str, Any]:
    """
    获取并验证某只股票的财报官网来源页面（IR 首页 / 财报页 / SEC filings 页）。

    说明：
    - 本接口采用模块化流程（候选发现 -> 页面抓取 -> 验证评分 -> 可选 AI 复核 -> 持久化）。
    - 不影响现有财报抓取、因子计算与批量刷新逻辑。
    """
    t = ticker.upper().strip()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required")
    service = _get_report_source_service()
    return await asyncio.to_thread(service.resolve, t, bool(force_refresh))


@app.post("/stockflow/report_source/batch_refresh", summary="批量获取财报官网来源并验证")
async def batch_refresh_report_source(payload: ReportSourceBatchRequest) -> Dict[str, Any]:
    """
    批量获取并验证财报官网来源，适合被 batch job 或运维脚本调用。
    """
    tickers = [str(t).strip().upper() for t in (payload.tickers or []) if str(t).strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="tickers is required")
    service = _get_report_source_service()
    return await asyncio.to_thread(service.resolve_batch, tickers, bool(payload.force_refresh))


@app.get("/stockflow/report_source/catalog/list", summary="获取已缓存的财报官网来源目录")
async def list_report_source_catalog(
    limit: int = Query(500, ge=1, le=2000, description="最大返回条数"),
    ticker_prefix: str = Query("", description="按 ticker 前缀过滤"),
) -> Dict[str, Any]:
    service = _get_report_source_service()
    return await asyncio.to_thread(service.list_catalog, int(limit), ticker_prefix)


@app.get("/report_source/catalog", summary="财报官网来源目录页面", response_class=HTMLResponse)
async def report_source_catalog_page() -> HTMLResponse:
    default_list = ",".join(default_tickers[:20])
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Report Source Catalog</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f5f7fb; color: #0f172a; }}
    .wrap {{ max-width: 1280px; margin: 24px auto; padding: 0 16px; }}
    .panel {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; box-shadow: 0 6px 20px rgba(2, 6, 23, .04); }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    .row {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }}
    input, textarea {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 10px; font-size: 14px; }}
    input {{ width: 140px; }}
    textarea {{ width: 100%; min-height: 58px; }}
    button {{ border: 1px solid #0ea5e9; background: #0ea5e9; color: #fff; border-radius: 8px; padding: 8px 12px; cursor: pointer; font-weight: 600; }}
    button.alt {{ border-color: #94a3b8; background: #fff; color: #334155; }}
    button.warn {{ border-color: #f59e0b; background: #f59e0b; }}
    .meta {{ font-size: 12px; color: #64748b; margin-top: 8px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #eef2f7; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; position: sticky; top: 0; z-index: 2; }}
    .tag {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }}
    .ok {{ background: #dcfce7; color: #166534; }}
    .partial {{ background: #fef9c3; color: #854d0e; }}
    .nf {{ background: #fee2e2; color: #991b1b; }}
    .small {{ font-size: 12px; color: #64748b; }}
    .links a {{ display: block; max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #0369a1; text-decoration: none; }}
    .links a:hover {{ text-decoration: underline; }}
    .sticky {{ position: sticky; top: 0; background: #f5f7fb; padding: 8px 0 12px; z-index: 3; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="sticky">
      <h1>Report Source Catalog</h1>
      <div class="panel">
        <div class="row">
          <input id="limit" type="number" min="1" max="2000" value="500" />
          <input id="prefix" placeholder="Ticker prefix e.g. A" />
          <button id="loadCached">Load Cached Directory</button>
          <button id="loadSample" class="alt">Use Sample 20</button>
          <button id="resolveInput" class="warn">Resolve Input Tickers</button>
        </div>
        <textarea id="tickers" placeholder="Comma/space separated tickers, e.g. AAPL, MSFT, NVDA">{default_list}</textarea>
        <div class="meta" id="meta">Ready.</div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th style="width:88px;">Ticker</th>
          <th style="width:170px;">Company</th>
          <th style="width:86px;">Status</th>
          <th style="width:86px;">Conf.</th>
          <th>Links</th>
          <th style="width:120px;">Candidates</th>
          <th style="width:170px;">Discovered</th>
          <th style="width:90px;">Action</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <script>
    const meta = document.getElementById("meta");
    const rowsEl = document.getElementById("rows");
    const limitEl = document.getElementById("limit");
    const prefixEl = document.getElementById("prefix");
    const tickersEl = document.getElementById("tickers");
    const resolveInputBtn = document.getElementById("resolveInput");
    let currentItems = [];
    let isResolving = false;

    function parseTickers(raw) {{
      return Array.from(new Set(
        String(raw || "")
          .toUpperCase()
          .split(/[^A-Z0-9.^=-]+/)
          .map(s => s.trim())
          .filter(Boolean)
      ));
    }}

    function esc(s) {{
      return String(s || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    function statusTag(status) {{
      const s = String(status || "").toLowerCase();
      if (s === "verified") return `<span class="tag ok">verified</span>`;
      if (s === "partial") return `<span class="tag partial">partial</span>`;
      if (s === "error") return `<span class="tag nf">error</span>`;
      return `<span class="tag nf">${{esc(s || "n/a")}}</span>`;
    }}

    function normalizeItem(item) {{
      const out = Object.assign({{}}, item || {{}});
      if (out.candidate_count == null && out.evidence && typeof out.evidence === "object") {{
        out.candidate_count = out.evidence.candidate_count ?? "";
      }}
      return out;
    }}

    function linkBlock(item) {{
      const lines = [];
      if (item.ir_home_url) lines.push(`<a href="${{esc(item.ir_home_url)}}" target="_blank" rel="noopener">IR: ${{esc(item.ir_home_url)}}</a>`);
      if (item.financial_reports_url) lines.push(`<a href="${{esc(item.financial_reports_url)}}" target="_blank" rel="noopener">Reports: ${{esc(item.financial_reports_url)}}</a>`);
      if (item.sec_filings_url) lines.push(`<a href="${{esc(item.sec_filings_url)}}" target="_blank" rel="noopener">SEC: ${{esc(item.sec_filings_url)}}</a>`);
      if (item.error) lines.push(`<span class="small" style="color:#991b1b;">${{esc(item.error)}}</span>`);
      return lines.length ? `<div class="links">${{lines.join("")}}</div>` : `<span class="small">No verified links</span>`;
    }}

    function rowHtml(item) {{
      return `
        <tr>
          <td><strong>${{esc(item.ticker)}}</strong></td>
          <td>${{esc(item.company_name || "")}}</td>
          <td>${{statusTag(item.verification_status)}}</td>
          <td>${{Number(item.confidence || 0).toFixed(3)}}</td>
          <td>${{linkBlock(item)}}</td>
          <td>${{esc(item.candidate_count ?? "")}}</td>
          <td><span class="small">${{esc(item.discovered_at || "")}}</span></td>
          <td><button class="alt" onclick="refreshOne('${{esc(item.ticker)}}')">Refresh</button></td>
        </tr>
      `;
    }}

    function render(items) {{
      currentItems = (items || []).map(normalizeItem);
      rowsEl.innerHTML = currentItems.map(rowHtml).join("");
    }}

    function upsertItem(item, append = true) {{
      const normalized = normalizeItem(item);
      const ticker = String(normalized.ticker || "").toUpperCase();
      if (!ticker) return;
      normalized.ticker = ticker;

      const idx = currentItems.findIndex(x => String((x || {{}}).ticker || "").toUpperCase() === ticker);
      if (idx >= 0) {{
        currentItems[idx] = normalized;
      }} else if (append) {{
        currentItems.push(normalized);
      }} else {{
        currentItems.unshift(normalized);
      }}
      rowsEl.innerHTML = currentItems.map(rowHtml).join("");
    }}

    async function loadCached() {{
      const limit = Math.max(1, Math.min(2000, Number(limitEl.value || 500)));
      const prefix = encodeURIComponent(prefixEl.value || "");
      try {{
        meta.textContent = "Loading cached directory...";
        const res = await fetch(`/stockflow/report_source/catalog/list?limit=${{limit}}&ticker_prefix=${{prefix}}`);
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(String(data.detail || `HTTP ${{res.status}}`));
        }}
        const items = Array.isArray(data.items) ? data.items : [];
        render(items);
        meta.textContent = `Loaded ${{items.length}} records (cached).`;
      }} catch (err) {{
        meta.textContent = `Load cached failed: ${{String(err && err.message ? err.message : err)}}`;
      }}
    }}

    async function resolveInput() {{
      if (isResolving) {{
        meta.textContent = "Resolve is already running...";
        return;
      }}
      const tickers = parseTickers(tickersEl.value);
      if (!tickers.length) {{
        meta.textContent = "No valid tickers.";
        return;
      }}
      isResolving = true;
      resolveInputBtn.disabled = true;
      currentItems = [];
      rowsEl.innerHTML = "";

      let success = 0;
      let failed = 0;
      try {{
        for (let i = 0; i < tickers.length; i++) {{
          const ticker = tickers[i];
          meta.textContent = `Resolving ${{i + 1}}/${{tickers.length}}: ${{ticker}} (success ${{success}}, failed ${{failed}})...`;
          try {{
            const res = await fetch(`/stockflow/report_source/${{encodeURIComponent(ticker)}}?force_refresh=1`);
            const data = await res.json();
            if (!res.ok) {{
              throw new Error(String(data.detail || `HTTP ${{res.status}}`));
            }}
            upsertItem(data, true);
            success += 1;
          }} catch (err) {{
            failed += 1;
            upsertItem({{
              ticker,
              company_name: "",
              verification_status: "error",
              confidence: 0,
              candidate_count: "",
              discovered_at: new Date().toISOString(),
              error: String(err && err.message ? err.message : err),
            }}, true);
          }}
        }}
      }} finally {{
        isResolving = false;
        resolveInputBtn.disabled = false;
      }}
      meta.textContent = `Resolved ${{success}} / ${{tickers.length}} tickers, failed ${{failed}}.`;
    }}

    async function refreshOne(ticker) {{
      if (isResolving) {{
        meta.textContent = "Please wait for current resolve to finish.";
        return;
      }}
      meta.textContent = `Refreshing ${{ticker}}...`;
      try {{
        const res = await fetch(`/stockflow/report_source/${{encodeURIComponent(ticker)}}?force_refresh=1`);
        const item = await res.json();
        if (!res.ok) {{
          throw new Error(String(item.detail || `HTTP ${{res.status}}`));
        }}
        upsertItem(item, false);
        meta.textContent = `Refreshed ${{ticker}}.`;
      }} catch (err) {{
        meta.textContent = `Refresh failed for ${{ticker}}: ${{String(err && err.message ? err.message : err)}}`;
      }}
    }}
    window.refreshOne = refreshOne;

    document.getElementById("loadCached").addEventListener("click", loadCached);
    document.getElementById("resolveInput").addEventListener("click", resolveInput);
    document.getElementById("loadSample").addEventListener("click", () => {{
      tickersEl.value = "{default_list}";
    }});

    loadCached();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/ai_context/daily_index", summary="获取指定日期的AI context清单")
# --- API：获取指定日期的当日清单（给图卡批量使用） ---
async def get_daily_index(date: Optional[str] = Query(None, description="YYYY-MM-DD, 默认为今天")):
    """
    获取指定日期的 AI context 清单。
    此清单包含当日已处理的股票代码及其对应的 AI context 文件在 GCS 上的路径。
    如果未指定日期，则默认为今天。

    **主要行为：**
    *   **获取数据：** ❌ 不会获取股票的财报数据。
    *   **生成 AI Context：** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **检索：** ✅ 会从 GCS 检索并返回 `ai_context/daily_index/{YYYY-MM-DD}.json` 文件内容，其中包含当日已处理股票的 AI Context 路径列表。

    **用途：**
    供图卡系统或其他下游服务批量获取当日已更新的所有 AI Context 文件路径，以便进行后续处理（如批量喂给 LLM）。
    """
    date = date or _today_str()
    storage_client = storage.Client()
    bucket = storage_client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(f"ai_context/daily_index/{date}.json")
    if not blob.exists():
        return {"date": date, "items": []}
    try:
        items = json.loads(blob.download_as_text())
        return {"date": date, "items": items}
    except Exception as exc:
        logger.error("Failed to load daily index for %s: %s", date, exc)
        raise HTTPException(status_code=500, detail=f"Failed to load daily index for {date}: {exc}")


@app.get("/ai_context/{ticker}/by_date/{date}", summary="获取某股某日AI context路径")
# --- API：获取某 ticker 在某日的单个 context 路径（给 QA 问答使用） ---
async def get_ai_context_path(ticker: str, date: str):
    """
    获取指定股票在指定日期的 AI context 文件在 GCS 上的路径。
    此接口主要用于 QA 或需要精确查找特定日期 Context 的场景，下游服务无需自行拼接 GCS 路径。
    
    **主要行为：**
    *   **获取数据：** ❌ 不会。
    *   **生成 AI Context：** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **提供路径：** ✅ 仅根据提供的股票代码和日期构造并返回对应的 AI Context 文件在 GCS 上的路径（`gs://<bucket_name>/ai_context/{TICKER}/{YYYY-MM-DD}.txt`）。

    **用途：**
    供 QA 或需要精确查找特定股票在特定日期的 AI Context 文件的场景使用。调用方需要自行去 GCS 读取该路径下的内容。
    """
    ticker = ticker.upper()
    gs_path = f"gs://{GCS_BUCKET_NAME}/ai_context/{ticker}/{date}.txt"
    
    # 可选：检查文件是否存在。为了快速响应，这里不强制检查文件存在性，
    # 而是直接返回路径，由调用方处理文件不存在的情况。
    # storage_client = storage.Client()
    # bucket = storage_client.bucket(GCS_BUCKET_NAME)
    # blob = bucket.blob(f"ai_context/{ticker}/{date}.txt")
    # if not blob.exists():
    #     raise HTTPException(status_code=404, detail=f"AI context file not found for {ticker} on {date}.")
    
    return {"ticker": ticker, "date": date, "path": gs_path}


@app.get("/health", summary="健康检查")
# --- API：健康检查 ---
async def health() -> Dict[str, str]:
    """
    健康检查端点。
    返回一个简单的 JSON 对象表示服务处于运行状态。供 Cloud Run 等平台进行健康监测。
    
    **主要行为：**
    *   **获取数据：** ❌ 不会。
    *   **生成 AI Context：** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **状态检查：** ✅ 返回服务运行状态的简单指示。

    **用途：**
    供部署平台（如 Cloud Run）和监控系统检查服务是否正常运行。
    """
    return {"status": "ok"}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    # 为了在本地测试时看到启动日志，可以调整日志级别
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
