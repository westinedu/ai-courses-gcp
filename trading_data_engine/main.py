from __future__ import annotations

import json
import os
import logging
import time
from datetime import datetime, date, timedelta
from threading import Event, Lock
from typing import Dict, List, Optional, Any

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Body # Added Body for batch_refresh
from fastapi.responses import JSONResponse
import pytz
from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

request_logger = logging.getLogger("request_logger")
request_logger.setLevel(logging.INFO)
if not request_logger.handlers:
    request_logger.addHandler(logging.StreamHandler())
request_logger.propagate = False # Prevent duplicate logging if root logger also configured to stdout

# --- GCS Configuration ---
# GCS bucket for all data (historical and AI context). Get from environment variable for Cloud Run deployment.
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
if not GCS_BUCKET_NAME:
    logger.warning("GCS_BUCKET_NAME environment variable not set. Data will NOT be persisted to GCS.")
    # Fallback for local testing if GCS not configured
    LOCAL_FALLBACK_DATA_DIR = os.path.join(os.path.dirname(__file__), "historical_data_local_fallback")
    LOCAL_FALLBACK_AI_CONTEXT_DIR = os.path.join(os.path.dirname(__file__), "ai_context_local_fallback")
    LOCAL_FALLBACK_DAILY_INDEX_DIR = os.path.join(LOCAL_FALLBACK_AI_CONTEXT_DIR, "daily_index") # For daily_index.json
    LOCAL_FALLBACK_ANALYSIS_DIR = os.path.join(os.path.dirname(__file__), "analysis_local_fallback")
    LOCAL_FALLBACK_ANALYSIS_INDEX_DIR = os.path.join(LOCAL_FALLBACK_ANALYSIS_DIR, "daily_index")

    os.makedirs(LOCAL_FALLBACK_DATA_DIR, exist_ok=True)
    os.makedirs(LOCAL_FALLBACK_AI_CONTEXT_DIR, exist_ok=True)
    os.makedirs(LOCAL_FALLBACK_DAILY_INDEX_DIR, exist_ok=True)
    os.makedirs(LOCAL_FALLBACK_ANALYSIS_DIR, exist_ok=True)
    os.makedirs(LOCAL_FALLBACK_ANALYSIS_INDEX_DIR, exist_ok=True)

# GCS path for the dynamic ticker list file
GCS_TICKER_LIST_BLOB_NAME = os.environ.get("GCS_TICKER_LIST_BLOB", "config/default_tickers.json")
GENERATE_ANALYSIS_IN_BATCH = str(os.environ.get("GENERATE_ANALYSIS_IN_BATCH", "1")).strip().lower() not in {"0", "false", "no"}


# --- FastAPI App Initialization ---
app = FastAPI(title="Trading Data Service",
              description="提供股票行情、历史 K 线及特征工程数据的服务。",
              version="1.0.0")

# --- In-process de-dup / throttling (best-effort; per Cloud Run instance) ---
_REFRESH_LOCK = Lock()
_REFRESH_INFLIGHT: Dict[str, Event] = {}
_REFRESH_LAST_OK_AT: Dict[str, float] = {}
_REFRESH_LAST_FAIL_AT: Dict[str, float] = {}

_ANALYSIS_LOCK = Lock()
_ANALYSIS_INFLIGHT: Dict[str, Event] = {}

# --- Global Constants ---
# Timezone for scheduling and timestamps (e.g., US market close time)
TIMEZONE = pytz.timezone(os.environ.get("ENGINE_TZ", 'America/Los_Angeles')) # 从环境变量获取时区
_DEFAULT_HARDCODED_TICKERS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "NVDA",
    "AMD",
    "JPM",
    "V",
    "BRK-B",
    "WMT",
    "COST",
    "KO",
    "NKE",
    "LLY",
    "UNH",
    "CAT",
    "DIS",
    "NFLX",
    # 财富自由图卡覆盖的 ETF / 股票
    "VOO",
    "QQQ",
    "ARKK",
    "ICLN",
    "AGG",
    "SNOW",
    "CRWD",
    "IHI",
    "GLD",
    "DVY",
    "HDV",
    "TLT",
    "BND",
    "VNQ",
    "3690.HK",
    # ==================================================
    # 宏观经济指标 Ticker 列表
    # ==================================================

    # --- 美元指数 ---
    # 衡量美元对一篮子主要货币的强度
    "DX-Y.NYB",

    # --- 美债收益率 (Yields) ---
    # 注意：这些是百分比收益率，不是价格
    # 关键的宏观经济指标，全球资产定价之锚
    "^TNX",      # 10年期美债收益率 (10-Year Treasury Yield)
    "^TYX",      # 30年期美债收益率 (30-Year Treasury Yield)
    "^FVX",      # 5年期美债收益率 (5-Year Treasury Yield)
    "^IRX",      # 13周短期国库券收益率 (13-Week Treasury Bill)

    # --- 美债价格ETF (Prices) ---
    # 用于追踪实际的美债市场价格走势
    "TLT",       # 20+年期长期美国国债ETF (iShares 20+ Year Treasury Bond ETF)
    "IEF",       # 7-10年中期美国国债ETF (iShares 7-10 Year Treasury Bond ETF)
    "SHY",       # 1-3年短期美国国债ETF (iShares 1-3 Year Treasury Bond ETF)

] # 硬编码的默认股票列表，作为fallback
_CURRENT_ACTIVE_TICKERS: List[str] = [] # 全局变量，用于存储动态加载或更新的股票列表

# 财富自由图卡：前端请求的 Ticker 与批量数据/ yfinance 使用的符号映射
_PORTFOLIO_TICKER_ALIASES = {
    "BRK.B": "BRK-B",
    "SEHK:3690": "3690.HK",
}

# 前端中的占位符资产（无需行情源）
_STATIC_PORTFOLIO_TICKERS = {"CASH"}


# --- Helper Functions ---

def is_trading_day(check_date: date) -> bool:
    """
    非常基本的交易日检查，只排除周末。
    对于实际应用，需要考虑节假日和市场休市。
    """
    return check_date.weekday() < 5 # Monday=0, Sunday=6


def _latest_expected_trading_day(today_in_tz: date) -> date:
    d = today_in_tz
    while not is_trading_day(d):
        d = d - timedelta(days=1)
    return d


def _maybe_refresh_daily_once(ticker: str, min_interval_seconds: int = 600, fail_backoff_seconds: int = 60) -> None:
    """
    Best-effort: ensure only one request per instance refreshes a ticker within a short time window.
    This is NOT a distributed lock; use it to reduce duplicate yfinance calls under burst traffic.
    """
    t = ticker.upper()
    now = time.time()

    with _REFRESH_LOCK:
        last_ok = _REFRESH_LAST_OK_AT.get(t, 0.0)
        if now - last_ok < float(min_interval_seconds):
            return
        last_fail = _REFRESH_LAST_FAIL_AT.get(t, 0.0)
        if now - last_fail < float(fail_backoff_seconds):
            return

        inflight = _REFRESH_INFLIGHT.get(t)
        if inflight is not None:
            wait_ev = inflight
        else:
            wait_ev = None
            ev = Event()
            _REFRESH_INFLIGHT[t] = ev

    if wait_ev is not None:
        # Wait a bit so the next request can reuse fresh storage.
        wait_ev.wait(timeout=12.0)
        return

    ok = False
    try:
        daily_incremental_update_job(t)
        ok = True
    finally:
        with _REFRESH_LOCK:
            if ok:
                _REFRESH_LAST_OK_AT[t] = time.time()
            else:
                _REFRESH_LAST_FAIL_AT[t] = time.time()
            done = _REFRESH_INFLIGHT.pop(t, None)
            if done is not None:
                done.set()

def _fetch_quote(ticker: str) -> Dict:
    """
    使用 `yfinance` 获取指定股票的实时行情信息。
    """
    stock = yf.Ticker(ticker)
    info: Dict = {}
    try:
        info = stock.info
    except Exception as exc:
        logger.warning("获取 %s 行情信息失败: %s", ticker, exc)
    result = {
        "ticker": ticker,
        "price": info.get("regularMarketPrice"),
        "previous_close": info.get("previousClose"),
        "open": info.get("open"),
        "day_high": info.get("dayHigh"),
        "day_low": info.get("dayLow"),
        "volume": info.get("volume"),
        "currency": info.get("currency"),
        "last_updated": datetime.now(TIMEZONE).isoformat(),
    }
    return result


def _resolve_portfolio_ticker(raw_ticker: str) -> Optional[str]:
    """将前端传入的组合 ticker 映射为批量数据使用的符号。"""
    if not raw_ticker:
        return None

    key = raw_ticker.strip().upper()
    if not key:
        return None

    if key in _STATIC_PORTFOLIO_TICKERS:
        return None

    return _PORTFOLIO_TICKER_ALIASES.get(key, key)


def _load_latest_price_snapshot(ticker: str) -> Optional[Dict[str, float]]:
    """从批量存储的历史数据中提取最新收盘价和日涨跌幅。"""
    df = _load_historical_data_from_storage(ticker)
    if df.empty:
        logger.warning("未在存储中找到 %s 的历史数据，无法生成财富图卡行情。", ticker)
        return None

    df = df.sort_index()
    latest = df.iloc[-1]
    close = latest.get("Close")

    try:
        price = round(float(close), 2)
    except (TypeError, ValueError):
        logger.error("%s 最新收盘价不可解析: %s", ticker, close)
        return None

    change_percent = 0.0
    if len(df) > 1:
        prev_close_raw = df.iloc[-2].get("Close")
        try:
            prev_close = float(prev_close_raw)
            if prev_close not in (0.0, -0.0):
                change_percent = round(((price - prev_close) / prev_close) * 100, 2)
        except (TypeError, ValueError):
            logger.warning("%s 前一日收盘价不可解析，涨跌幅置为 0。原始值: %s", ticker, prev_close_raw)

    return {
        "price": price,
        "changePercent": change_percent,
    }


def _fetch_historical_df(
    ticker: str,
    period: Optional[str] = None, # Use period or start/end
    interval: str = '1d',
    start: Optional[date] = None,
    end: Optional[date] = None
) -> pd.DataFrame:
    """
    使用 `yfinance` 获取指定股票的历史价格数据为 DataFrame。
    返回带有 **timezone-naive DatetimeIndex** 的 DataFrame。
    优先使用 start/end 参数，如果提供 period，则使用 period。
    """
    stock = yf.Ticker(ticker)
    try:
        if start and end:
            # yfinance start/end parameters are inclusive
            df = stock.history(start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), interval=interval)
        elif period:
            df = stock.history(period=period, interval=interval)
        else:
            logger.warning("必须提供 period 或 start/end 日期参数来获取历史数据。")
            return pd.DataFrame()

        if df.empty:
            logger.warning("获取 %s 历史数据为空，周期: %s, 间隔: %s, 开始: %s, 结束: %s", ticker, period, interval, start, end)
            return pd.DataFrame()
        
        # --- 核心改动：将 DatetimeIndex 转换为 timezone-naive ---
        # yfinance 默认返回的是 timezone-aware DatetimeIndex，通常是交易所时区
        # 为了与从GCS加载的日期字符串（通常是Naive）兼容，我们将其转换为Naive
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            # 首先转换为 UTC，然后移除时区信息，这样保证跨时区一致性
            df.index = df.index.tz_convert('UTC').tz_localize(None)
            logger.debug("将 %s 的历史数据索引转换为 timezone-naive UTC。", ticker)
        
        return df
    except Exception as exc:
        logger.error("获取 %s 历史数据失败 (周期: %s, 间隔: %s, 开始: %s, 结束: %s): %s", ticker, period, interval, start, end, exc)
        return pd.DataFrame()


# --- API Contract Helpers (compatible with vercel-nextjs /us-stocks) ---

def _normalize_symbol(raw: Any) -> str:
    v = str(raw or "AAPL").strip().upper()
    # Yahoo symbol format is allowed: letters, numbers, dash, dot, caret.
    if 1 <= len(v) <= 12 and all(ch.isalnum() or ch in ".^-" for ch in v):
        return v
    return "AAPL"


def _clamp_int(value: Any, fallback: int, min_v: int, max_v: int) -> int:
    try:
        n = int(float(value))
    except Exception:
        return fallback
    return max(min_v, min(max_v, n))


def _years_to_range(years: int) -> str:
    if years <= 1:
        return "1y"
    if years <= 2:
        return "2y"
    if years <= 5:
        return "5y"
    return "10y"


def _sigmoid(x: float) -> float:
    try:
        return float(1.0 / (1.0 + np.exp(-x)))
    except Exception:
        return 0.5


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _df_to_market_candles(df: pd.DataFrame, limit: int) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    df2 = df.copy()
    # df index is Date; keep last N rows.
    df2 = df2.sort_index()
    if limit > 0:
        df2 = df2.tail(limit)

    out: List[Dict[str, Any]] = []
    for idx, row in df2.iterrows():
        # idx is naive datetime at 00:00:00 UTC-ish; treat as UTC.
        try:
            t = _to_ms(datetime(idx.year, idx.month, idx.day, tzinfo=pytz.UTC))
        except Exception:
            continue
        def _n(v: Any) -> Optional[float]:
            try:
                fv = float(v)
                return fv if np.isfinite(fv) else None
            except Exception:
                return None

        out.append(
            {
                "t": t,
                "o": _n(row.get("Open")),
                "h": _n(row.get("High")),
                "l": _n(row.get("Low")),
                "c": _n(row.get("Close")),
                "v": _n(row.get("Volume")),
            }
        )
    # filter incomplete rows
    return [c for c in out if c.get("c") is not None]


def _default_factor_weights() -> Dict[str, float]:
    return {
        "rsi14": 0.22,
        "macdHist": 0.30,
        "ema200Trend": 0.22,
        "momentum20": 0.16,
        "volumeTrend": 0.10,
        "user": 0.0,
    }


def _factor_stance(score: float, deadband: float = 0.12) -> str:
    if score >= deadband:
        return "bullish"
    if score <= -deadband:
        return "bearish"
    return "neutral"


def _compute_analysis_from_df(
    symbol: str,
    df: pd.DataFrame,
    years: int,
    weights_override: Optional[Dict[str, Any]] = None,
    user_factor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    df2 = df.copy()
    if df2.empty or "Close" not in df2.columns:
        raise ValueError("Not enough candles for analysis.")

    df2 = df2.sort_index()
    close = pd.to_numeric(df2["Close"], errors="coerce")
    volume = pd.to_numeric(df2.get("Volume"), errors="coerce") if "Volume" in df2.columns else pd.Series(dtype=float)

    # Required windows: allow partial availability, produce neutral scores when missing.
    rsi_series = _compute_rsi(close.dropna(), 14) if close.notna().sum() >= 30 else pd.Series(dtype=float)
    macd_df = _compute_macd(close.dropna()) if close.notna().sum() >= 40 else pd.DataFrame()
    ema200 = close.rolling(window=200).mean()

    latest_close = float(close.iloc[-1]) if np.isfinite(close.iloc[-1]) else None
    if latest_close is None:
        raise ValueError("Not enough candles for analysis.")

    as_of_ms = _to_ms(datetime(df2.index[-1].year, df2.index[-1].month, df2.index[-1].day, tzinfo=pytz.UTC))

    # Factors
    factors: List[Dict[str, Any]] = []

    # RSI(14)
    rsi_val = float(rsi_series.iloc[-1]) if len(rsi_series) and np.isfinite(rsi_series.iloc[-1]) else None
    rsi_score = 0.0
    if rsi_val is not None:
        if rsi_val <= 30:
            rsi_score = 0.5 + 0.5 * (30 - rsi_val) / 30
        elif rsi_val >= 70:
            rsi_score = -0.5 - 0.5 * (rsi_val - 70) / 30
        else:
            # map 30..70 to -0.5..+0.5 around 50
            rsi_score = (rsi_val - 50) / 40
        rsi_score = float(np.clip(rsi_score, -1, 1))

    factors.append(
        {
            "id": "rsi14",
            "label": "RSI(14)",
            "value": rsi_val,
            "score": rsi_score,
            "stance": _factor_stance(rsi_score),
            "explanation": "RSI < 30 tends to be oversold (bullish), RSI > 70 tends to be overbought (bearish).",
        }
    )

    # MACD histogram
    hist_val = None
    macd_score = 0.0
    if not macd_df.empty and "hist" in macd_df.columns:
        hist_series = pd.to_numeric(macd_df["hist"], errors="coerce")
        hist_val_raw = hist_series.iloc[-1] if len(hist_series) else np.nan
        if np.isfinite(hist_val_raw):
            hist_val = float(hist_val_raw)
            # normalize by recent stdev to keep stable across symbols
            recent = hist_series.tail(120).dropna()
            denom = float(recent.std()) if len(recent) >= 30 and np.isfinite(recent.std()) else 1.0
            denom = max(1e-9, denom)
            macd_score = float(np.tanh(hist_val / (denom * 2.0)))

    factors.append(
        {
            "id": "macdHist",
            "label": "MACD Hist",
            "value": hist_val,
            "score": macd_score,
            "stance": _factor_stance(macd_score),
            "explanation": "MACD histogram measures momentum (positive = bullish, negative = bearish).",
        }
    )

    # EMA200 trend (percent distance)
    ema_val = ema200.iloc[-1] if len(ema200) else np.nan
    ema_score = 0.0
    ema_dist = None
    if np.isfinite(ema_val) and latest_close:
        ema_dist = float(latest_close / float(ema_val) - 1.0)
        ema_score = float(np.tanh(ema_dist * 8.0))

    factors.append(
        {
            "id": "ema200Trend",
            "label": "EMA200 Trend",
            "value": ema_dist,
            "score": ema_score,
            "stance": _factor_stance(ema_score),
            "explanation": "Price vs EMA200 approximates long-term trend (above = bullish, below = bearish).",
        }
    )

    # Momentum 20D
    mom_score = 0.0
    mom_val = None
    if close.notna().sum() >= 25:
        prev = close.iloc[-21]
        if np.isfinite(prev) and float(prev) != 0.0:
            mom_val = float(latest_close / float(prev) - 1.0)
            mom_score = float(np.tanh(mom_val * 10.0))

    factors.append(
        {
            "id": "momentum20",
            "label": "Momentum(20D)",
            "value": mom_val,
            "score": mom_score,
            "stance": _factor_stance(mom_score),
            "explanation": "20D return is a simple momentum proxy (positive = bullish).",
        }
    )

    # Volume trend (ratio to 20D avg)
    vol_score = 0.0
    vol_val = None
    if not volume.empty and volume.notna().sum() >= 25:
        vol_avg = volume.rolling(window=20).mean().iloc[-1]
        vol_latest = volume.iloc[-1]
        if np.isfinite(vol_avg) and np.isfinite(vol_latest) and float(vol_avg) > 0:
            vol_val = float(vol_latest / float(vol_avg))
            # > 1 means higher-than-usual participation
            vol_score = float(np.tanh((vol_val - 1.0) * 1.5))

    factors.append(
        {
            "id": "volumeTrend",
            "label": "Volume Trend",
            "value": vol_val,
            "score": vol_score,
            "stance": _factor_stance(vol_score),
            "explanation": "Volume vs 20D average as a participation proxy.",
        }
    )

    # User factor (optional)
    user_score = 0.0
    user_value = None
    user_expl = ""
    if isinstance(user_factor, dict):
        stance = user_factor.get("stance")
        try:
            stance_n = int(stance)
        except Exception:
            stance_n = 0
        stance_n = int(np.clip(stance_n, -1, 1))
        user_value = stance_n
        user_score = float(stance_n)
        note = str(user_factor.get("note") or "").strip()
        if note:
            user_expl = note
    factors.append(
        {
            "id": "user",
            "label": "User Factor",
            "value": user_value,
            "score": user_score,
            "stance": _factor_stance(user_score, deadband=0.01),
            "explanation": user_expl or "User-provided stance adjustment.",
        }
    )

    weights = _default_factor_weights()
    if isinstance(weights_override, dict):
        for k, v in weights_override.items():
            try:
                weights[str(k)] = float(v)
            except Exception:
                continue

    # Compute contributions
    agg_score = 0.0
    for f in factors:
        w = float(weights.get(f["id"], 0.0))
        f["weight"] = w
        f["contribution"] = float(w) * float(f["score"] or 0.0)
        agg_score += f["contribution"]

    # Simple probability mapping (MVP).
    p_up = float(_sigmoid(agg_score * 1.6))
    p_down = float(1.0 - p_up)
    confidence = float(abs(p_up - 0.5) * 2.0)
    signal = "HOLD"
    if p_up > 0.6:
        signal = "BUY"
    elif p_up < 0.4:
        signal = "SELL"

    return {
        "symbol": symbol,
        "interval": "1d",
        "asOf": {"t": as_of_ms, "close": float(latest_close)},
        "candles": {
            "count": int(len(df2)),
            "from": df2.index[0].strftime("%Y-%m-%d") if len(df2.index) else None,
            "to": df2.index[-1].strftime("%Y-%m-%d") if len(df2.index) else None,
        },
        "aggregate": {
            "score": float(agg_score),
            "pUp": p_up,
            "pDown": p_down,
            "signal": signal,
            "confidence": confidence,
        },
        "factors": factors,
        "meta": {
            "provider": "yfinance",
            "years": years,
            "range": _years_to_range(years),
            "fetchedAt": datetime.now(TIMEZONE).isoformat(),
        },
    }

# --- Feature Engineering Functions ---

def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """计算相对强弱指数 (RSI)。"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _compute_macd(series: pd.Series) -> pd.DataFrame:
    """计算移动平均线收敛散度 (MACD) 指标。"""
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "hist": histogram
    })


def _compute_features(df: pd.DataFrame) -> Dict:
    """从历史价格 DataFrame 中派生多个技术指标和信号。"""
    features: Dict[str, Optional[float] | str] = {}
    if df.empty:
        return features
    try:
        df_copy = df.copy() # 确保不修改原始DataFrame
        
        # 确保 'Close' 是数值类型，并且可以被索引或作为列访问
        if 'Close' not in df_copy.columns:
            logger.error("DataFrame中未找到 'Close' 列，无法计算特征。")
            return {}

        df_copy['Close'] = pd.to_numeric(df_copy['Close'], errors='coerce')
        close = df_copy['Close'].dropna() # 删除Close价格中的NaN以便计算
        
        if close.empty or len(close) < 200: # 至少需要200个数据点来计算200日均线等
            logger.warning("没有足够的有效收盘价数据 (%d) 来计算全面的特征。", len(close))
            return {}

        # 计算收益率和均线（将日收益率转换为百分比形式以便直接展示）
        df_copy['return_1d'] = close.pct_change() * 100
        df_copy['ma_20'] = close.rolling(window=20).mean()
        df_copy['ma_50'] = close.rolling(window=50).mean()
        df_copy['ma_200'] = close.rolling(window=200).mean()
        # RSI
        df_copy['rsi'] = _compute_rsi(close)
        # MACD
        macd_df = _compute_macd(close)
        df_copy = pd.concat([df_copy, macd_df], axis=1) # 按列合并MACD结果
        
        # 确保我们使用最新的可用数据，处理因滚动窗口可能导致的末尾 NaN
        latest = df_copy.iloc[-1] 

        # 趋势判定：最近 10 天收盘价线性回归斜率
        trend = "unknown"
        if len(close) >= 10:
            recent = close.tail(10)
            x = np.arange(len(recent)) # 使用 numpy arange 生成 x 值
            # 确保有足够的数据点进行线性回归，避免 LinAlgError: Singular matrix
            if len(x) > 1 and not (recent == recent.iloc[0]).all(): # 确保数据不全部相同
                try:
                    # np.polyfit 返回系数，[0] 是斜率
                    slope = np.polyfit(x, recent, 1)[0]
                    if slope > 0.001: # 设置一个小的阈值以避免噪音
                        trend = "up"
                    elif slope < -0.001:
                        trend = "down"
                    else:
                        trend = "flat"
                except np.linalg.LinAlgError:
                    trend = "flat" # 理论上上面已检查，但作为兜底
            else: # 数据点不足或数据全部相同
                trend = "flat"
        
        # 信号：黄金交叉 / 死亡交叉
        signal = "neutral"
        # 检查前一天和当天的数据是否可用以检测交叉
        if len(df_copy) >= 2 and pd.notnull(df_copy.iloc[-2].get('ma_50')) and pd.notnull(df_copy.iloc[-2].get('ma_200')):
            prev_ma50 = df_copy.iloc[-2]['ma_50']
            prev_ma200 = df_copy.iloc[-2]['ma_200']
            curr_ma50 = latest.get('ma_50')
            curr_ma200 = latest.get('ma_200')

            if pd.notnull(curr_ma50) and pd.notnull(curr_ma200):
                if prev_ma50 < prev_ma200 and curr_ma50 > curr_ma200:
                    signal = "golden_cross"  # 看多信号
                elif prev_ma50 > prev_ma200 and curr_ma50 < curr_ma200:
                    signal = "death_cross"  # 看空信号
                elif curr_ma50 > curr_ma200: # 如果已经处于黄金交叉状态
                    signal = "golden_cross_state"
                elif curr_ma50 < curr_ma200: # 如果已经处于死亡交叉状态
                    signal = "death_cross_state"
        
        # RSI 信号
        rsi_val = latest.get('rsi')
        rsi_signal = None
        if pd.notnull(rsi_val):
            if rsi_val > 70:
                rsi_signal = "overbought"
            elif rsi_val < 30:
                rsi_signal = "oversold"
            else:
                rsi_signal = "neutral"
        
        # 确保所有值都可 JSON 序列化 (浮点数, 整数, 字符串, 布尔值, None)
        return_1d_val = float(latest['return_1d']) if pd.notnull(latest['return_1d']) else None
        return_1d_percent = f"{round(return_1d_val, 2)}%" if return_1d_val is not None else None

        features = {
            "latest_close": float(latest['Close']) if pd.notnull(latest['Close']) else None,
            "return_1d": return_1d_val,
            "return_1d_percent": return_1d_percent,
            "ma_20": float(latest['ma_20']) if pd.notnull(latest['ma_20']) else None,
            "ma_50": float(latest['ma_50']) if pd.notnull(latest['ma_50']) else None,
            "ma_200": float(latest['ma_200']) if pd.notnull(latest['ma_200']) else None,
            "rsi": float(rsi_val) if pd.notnull(rsi_val) else None,
            "macd": float(latest['macd']) if pd.notnull(latest['macd']) else None,
            "macd_signal": float(latest['signal']) if pd.notnull(latest['signal']) else None,
            "macd_hist": float(latest['hist']) if pd.notnull(latest['hist']) else None,
            "trend": trend,
            "ma_signal": signal,
            "rsi_signal": rsi_signal,
        }
    except Exception as exc:
        logger.exception("计算技术指标失败: %s", exc)
    return features

def _resample_to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """
    将日 K 线历史数据重采样为月 K 线数据。
    假定 DataFrame 已经具有 DatetimeIndex (timezone-naive)。
    返回带有 DatetimeIndex (timezone-naive) 的 DataFrame。
    """
    if df.empty:
        return pd.DataFrame()

    if not isinstance(df.index, pd.DatetimeIndex):
        logger.error("DataFrame 的索引不是 DatetimeIndex，无法进行月度重采样。")
        return pd.DataFrame()

    # 重采样到每月最后一个交易日
    # Pandas 2.2+ 中 'M' 已弃用/移除，需使用 'ME'（month end）
    monthly_df = df.resample('ME').agg(
        Open=('Open', 'first'),
        High=('High', 'max'),
        Low=('Low', 'min'),
        Close=('Close', 'last'),
        Volume=('Volume', 'sum')
    )

    # 删除所有 OHLCV 都是 NaN 的行（例如没有交易数据的月份）
    monthly_df = monthly_df.dropna(how='all', subset=['Open', 'High', 'Low', 'Close', 'Volume'])
    
    # 将 NaN 替换为 None，以便后续 JSON 兼容
    monthly_df = monthly_df.replace({np.nan: None})
    return monthly_df


# --- Historical Data Storage and Update Logic (GCS Integrated) ---

def _get_gcs_blob_name(ticker: str) -> str:
    """辅助函数，获取股票历史 JSON 文件的 GCS Blob 名称。"""
    return f"historical_data/{ticker.upper()}_historical.json"

def _get_local_fallback_filepath(ticker: str) -> str:
    """本地测试 fallback 路径辅助函数，用于历史数据。"""
    return os.path.join(LOCAL_FALLBACK_DATA_DIR, f"{ticker.upper()}_historical.json")


def _save_historical_data_to_storage(ticker: str, df: pd.DataFrame) -> str:
    """将历史数据 DataFrame 保存到 GCS 或本地 fallback。"""
    
    df_copy = df.copy()
    # 确保索引被重置，并且 'Date' 列被转换为字符串以便 JSON 序列化
    if isinstance(df_copy.index, pd.DatetimeIndex):
        df_copy = df_copy.reset_index()
        # 日期字符串保持为 naive 格式 (YYYY-MM-DD)，与加载时一致
        df_copy['Date'] = df_copy['Date'].dt.strftime("%Y-%m-%d")
    
    # 将任何 numpy.NaN 替换为 None 以便 JSON 兼容
    data_to_save = df_copy.replace({np.nan: None}).to_dict(orient="records")
    json_content = json.dumps(data_to_save, indent=2, ensure_ascii=False)

    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_blob_name(ticker)
            blob = bucket.blob(blob_name)
            blob.cache_control = "public, max-age=600, stale-while-revalidate=86400"
            
            blob.upload_from_string(json_content, content_type="application/json")
            logger.info("成功保存 %s 历史数据到 GCS: gs://%s/%s", ticker, GCS_BUCKET_NAME, blob_name)
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("保存 %s 历史数据到 GCS 失败: %s", ticker, exc)
            return ""
    else:
        filepath = _get_local_fallback_filepath(ticker)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_content)
            logger.warning("GCS_BUCKET_NAME 未设置。已保存 %s 历史数据到本地 fallback: %s (在 Cloud Run 中不持久化)", ticker, filepath)
            return filepath
        except Exception as exc:
            logger.error("保存 %s 历史数据到本地 fallback 文件 %s 失败: %s", ticker, filepath, exc)
            return ""


def _load_historical_data_from_storage(ticker: str) -> pd.DataFrame:
    """从 GCS 或本地 fallback 加载历史数据到 DataFrame。
    返回带有 **timezone-naive DatetimeIndex** 的 DataFrame。
    """
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_blob_name(ticker)
            blob = bucket.blob(blob_name)
            
            if not blob.exists():
                logger.info("在 GCS 中未找到 %s 的现有 Blob 文件: gs://%s/%s。", ticker, GCS_BUCKET_NAME, blob_name)
                return pd.DataFrame()
            
            json_content = blob.download_as_text(encoding="utf-8")
            data = json.loads(json_content)
            df = pd.DataFrame(data)
            
            if 'Date' in df.columns:
                # 确保加载后，索引也是 timezone-naive DatetimeIndex
                df['Date'] = pd.to_datetime(df['Date']) # This naturally creates naive datetimes from YYYY-MM-DD
                df = df.set_index('Date') # 设置索引以便后续合并/比较
            logger.info("成功从 GCS 加载 %s 历史数据。", ticker)
            return df
        except Exception as exc:
            logger.error("从 GCS 加载 %s 历史数据失败: %s", ticker, exc)
            return pd.DataFrame()
    else:
        filepath = _get_local_fallback_filepath(ticker)
        if not os.path.exists(filepath):
            logger.info("GCS_BUCKET_NAME 未设置。在本地未找到 %s 的现有 fallback 文件: %s。", ticker, filepath)
            return pd.DataFrame()
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            df = pd.DataFrame(data)
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.set_index('Date')
            logger.warning("GCS_BUCKET_NAME 未设置。已从本地 fallback 加载 %s 历史数据: %s", ticker, filepath)
            return df
        except Exception as exc:
            logger.error("从本地 fallback 文件 %s 加载 %s 历史数据失败: %s", filepath, ticker, exc)
            return pd.DataFrame()


# --- AI Context Data Storage and Logic (TXT format, matching Financial Engine) ---
# AI Context file path: ai_context/{TICKER}/{YYYY-MM-DD}.txt
def _get_gcs_ai_context_blob_name(ticker: str, context_date: date) -> str:
    """辅助函数，获取股票 AI Context JSON 文件的 GCS Blob 名称。"""
    return f"ai_context/{ticker.upper()}/{context_date.strftime('%Y-%m-%d')}.json"

def _get_local_fallback_ai_context_filepath(ticker: str, context_date: date) -> str:
    """本地测试 fallback 路径辅助函数，用于 AI Context JSON 文件。"""
    ticker_dir = os.path.join(LOCAL_FALLBACK_AI_CONTEXT_DIR, ticker.upper())
    os.makedirs(ticker_dir, exist_ok=True)
    return os.path.join(ticker_dir, f"{context_date.strftime('%Y-%m-%d')}.json")

def _save_ai_context_to_storage(ticker: str, context_date: date, ai_context_data: Dict) -> str:
    """将 AI Context 数据 (dict) 保存为 JSON 文件到 GCS 或本地 fallback。"""
    # 将 dict 转换为可读的 JSON 字符串以便写入 .txt 文件
    json_content_str = json.dumps(ai_context_data, indent=2, ensure_ascii=False)

    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_ai_context_blob_name(ticker, context_date)
            blob = bucket.blob(blob_name)
            
            blob.upload_from_string(json_content_str, content_type="application/json")
            logger.info("成功保存 %s AI Context 到 GCS: gs://%s/%s", ticker, GCS_BUCKET_NAME, blob_name)
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("保存 %s AI Context 到 GCS 失败: %s", ticker, exc)
            return ""
    else:
        filepath = _get_local_fallback_ai_context_filepath(ticker, context_date)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_content_str)
            logger.warning("GCS_BUCKET_NAME 未设置。已保存 %s AI Context 到本地 fallback: %s (在 Cloud Run 中不持久化)", ticker, filepath)
            return filepath
        except Exception as exc:
            logger.error("保存 %s AI Context 到本地 fallback 文件 %s 失败: %s", ticker, filepath, exc)
            return ""

# --- Daily Index for AI Context (Matching Financial Engine) ---
def _get_gcs_daily_index_blob_name(index_date: date) -> str:
    """辅助函数，获取每日 AI Context 索引 JSON 文件的 GCS Blob 名称。"""
    return f"ai_context/daily_index/{index_date.strftime('%Y-%m-%d')}.json"

def _get_local_daily_index_filepath(index_date: date) -> str:
    """本地测试 fallback 路径辅助函数，用于每日 AI Context 索引 JSON 文件。"""
    os.makedirs(LOCAL_FALLBACK_DAILY_INDEX_DIR, exist_ok=True) # 确保目录存在
    return os.path.join(LOCAL_FALLBACK_DAILY_INDEX_DIR, f"{index_date.strftime('%Y-%m-%d')}.json")

def _load_daily_index_from_storage(index_date: date) -> List[Dict]:
    """从 GCS 或本地 fallback 加载每日 AI Context 索引。"""
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_daily_index_blob_name(index_date)
            blob = bucket.blob(blob_name)
            
            if not blob.exists():
                logger.info("在 GCS 中未找到 %s 的现有每日索引 Blob 文件: gs://%s/%s。", index_date, GCS_BUCKET_NAME, blob_name)
                return []
            
            json_content = blob.download_as_text(encoding="utf-8")
            data = json.loads(json_content)
            logger.info("成功从 GCS 加载 %s 的每日索引。", index_date)
            return data
        except Exception as exc:
            logger.error("从 GCS 加载 %s 的每日索引失败: %s", index_date, exc)
            return []
    else:
        filepath = _get_local_daily_index_filepath(index_date)
        if not os.path.exists(filepath):
            logger.info("GCS_BUCKET_NAME 未设置。在本地未找到 %s 的现有每日索引文件: %s。", index_date, filepath)
            return []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.warning("GCS_BUCKET_NAME 未设置。已从本地 fallback 加载 %s 的每日索引: %s", index_date, filepath)
            return data
        except Exception as exc:
            logger.error("从本地 fallback 文件 %s 加载 %s 的每日索引失败: %s", filepath, index_date, exc)
            return []

def _save_daily_index_to_storage(index_date: date, index_list: List[Dict]) -> str:
    """将每日 AI Context 索引保存到 GCS 或本地 fallback。"""
    json_content = json.dumps(index_list, indent=2, ensure_ascii=False)

    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_daily_index_blob_name(index_date)
            blob = bucket.blob(blob_name)
            
            blob.upload_from_string(json_content, content_type="application/json")
            logger.info("成功保存 %s 的每日索引到 GCS: gs://%s/%s", index_date, GCS_BUCKET_NAME, blob_name)
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("保存 %s 的每日索引到 GCS 失败: %s", index_date, exc)
            return ""
    else:
        filepath = _get_local_daily_index_filepath(index_date)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_content)
            logger.warning("GCS_BUCKET_NAME 未设置。已保存 %s 的每日索引到本地 fallback: %s", index_date, filepath)
            return filepath
        except Exception as exc:
            logger.error("保存 %s 的每日索引到本地 fallback 文件 %s 失败: %s", filepath, index_date, exc)
            return ""


# --- Daily Analysis Storage (for Stockmap / probability graph) ---

def _get_gcs_analysis_blob_name(ticker: str, analysis_date: date) -> str:
    return f"analysis/{ticker.upper()}/{analysis_date.strftime('%Y-%m-%d')}.json"


def _get_gcs_analysis_daily_index_blob_name(index_date: date) -> str:
    return f"analysis/daily_index/{index_date.strftime('%Y-%m-%d')}.json"


def _get_local_fallback_analysis_filepath(ticker: str, analysis_date: date) -> str:
    ticker_dir = os.path.join(LOCAL_FALLBACK_ANALYSIS_DIR, ticker.upper())
    os.makedirs(ticker_dir, exist_ok=True)
    return os.path.join(ticker_dir, f"{analysis_date.strftime('%Y-%m-%d')}.json")


def _get_local_fallback_analysis_index_filepath(index_date: date) -> str:
    os.makedirs(LOCAL_FALLBACK_ANALYSIS_INDEX_DIR, exist_ok=True)
    return os.path.join(LOCAL_FALLBACK_ANALYSIS_INDEX_DIR, f"{index_date.strftime('%Y-%m-%d')}.json")


def _save_analysis_to_storage(ticker: str, analysis_date: date, analysis_data: Dict[str, Any]) -> str:
    json_content_str = json.dumps(analysis_data, indent=2, ensure_ascii=False)
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_analysis_blob_name(ticker, analysis_date)
            blob = bucket.blob(blob_name)
            blob.cache_control = "public, max-age=600, stale-while-revalidate=86400"
            blob.upload_from_string(json_content_str, content_type="application/json")
            logger.info("成功保存 %s analysis 到 GCS: gs://%s/%s", ticker, GCS_BUCKET_NAME, blob_name)
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("保存 %s analysis 到 GCS 失败: %s", ticker, exc)
            return ""
    filepath = _get_local_fallback_analysis_filepath(ticker, analysis_date)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(json_content_str)
        logger.warning("GCS_BUCKET_NAME 未设置。已保存 %s analysis 到本地 fallback: %s", ticker, filepath)
        return filepath
    except Exception as exc:
        logger.error("保存 %s analysis 到本地 fallback 文件 %s 失败: %s", ticker, filepath, exc)
        return ""


def _load_analysis_from_storage(ticker: str, analysis_date: date) -> Optional[Dict[str, Any]]:
    """Load analysis JSON from storage if present; return None when missing/unreadable."""
    t = ticker.upper()
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_analysis_blob_name(t, analysis_date)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                return None
            raw = blob.download_as_text(encoding="utf-8")
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception as exc:
            logger.error("从 GCS 加载 %s analysis 失败: %s", t, exc)
            return None

    filepath = _get_local_fallback_analysis_filepath(t, analysis_date)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception as exc:
        logger.error("从本地 fallback 文件 %s 加载 %s analysis 失败: %s", filepath, t, exc)
        return None


def _analysis_request_is_baseline(body: Dict[str, Any]) -> bool:
    """Only baseline requests are safe to cache at a fixed path (ticker/date)."""
    if not isinstance(body, dict):
        return True
    weights = body.get("weights")
    user_factor = body.get("userFactor")
    # If user passes custom weights or user factor, the output becomes user-specific -> do not cache globally.
    if isinstance(weights, dict) and len(weights):
        return False
    if isinstance(user_factor, dict) and len(user_factor):
        return False
    return True


def _try_save_analysis_if_absent(ticker: str, analysis_date: date, analysis_data: Dict[str, Any]) -> str:
    """
    Best-effort idempotent save: only create the object when it doesn't exist.
    If it already exists, return its gs:// path without overwriting.
    """
    t = ticker.upper()
    blob_name = _get_gcs_analysis_blob_name(t, analysis_date)
    if not GCS_BUCKET_NAME:
        return _save_analysis_to_storage(t, analysis_date, analysis_data)

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(blob_name)
        blob.cache_control = "public, max-age=600, stale-while-revalidate=86400"
        payload = json.dumps(analysis_data, indent=2, ensure_ascii=False)
        try:
            blob.upload_from_string(payload, content_type="application/json", if_generation_match=0)
            logger.info("成功创建 %s analysis 到 GCS: gs://%s/%s", t, GCS_BUCKET_NAME, blob_name)
        except PreconditionFailed:
            # Someone else already created it.
            pass
        return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
    except Exception as exc:
        logger.error("幂等保存 %s analysis 到 GCS 失败: %s", t, exc)
        return ""


def _load_analysis_daily_index_from_storage(index_date: date) -> List[Dict[str, Any]]:
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_analysis_daily_index_blob_name(index_date)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                return []
            json_content = blob.download_as_text(encoding="utf-8")
            data = json.loads(json_content)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error("从 GCS 加载 %s 的 analysis daily index 失败: %s", index_date, exc)
            return []

    filepath = _get_local_fallback_analysis_index_filepath(index_date)
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("从本地 fallback 文件 %s 加载 %s 的 analysis daily index 失败: %s", filepath, index_date, exc)
        return []


def _save_analysis_daily_index_to_storage(index_date: date, index_list: List[Dict[str, Any]]) -> str:
    json_content = json.dumps(index_list, indent=2, ensure_ascii=False)
    if GCS_BUCKET_NAME:
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob_name = _get_gcs_analysis_daily_index_blob_name(index_date)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(json_content, content_type="application/json")
            logger.info("成功保存 %s 的 analysis daily index 到 GCS: gs://%s/%s", index_date, GCS_BUCKET_NAME, blob_name)
            return f"gs://{GCS_BUCKET_NAME}/{blob_name}"
        except Exception as exc:
            logger.error("保存 %s 的 analysis daily index 到 GCS 失败: %s", index_date, exc)
            return ""

    filepath = _get_local_fallback_analysis_index_filepath(index_date)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(json_content)
        return filepath
    except Exception as exc:
        logger.error("保存 %s 的 analysis daily index 到本地 fallback 文件 %s 失败: %s", index_date, filepath, exc)
        return ""


def backfill_5_year_data_job(ticker: str) -> str:
    """
    获取指定股票 5 年的历史数据，并保存到 GCS (或本地 fallback)。
    如果成功则返回存储路径，否则返回空字符串。
    """
    ticker = ticker.upper()
    logger.info("开始为 %s 铺底 5 年数据...", ticker)
    # 明确指定起始和结束日期，以获取精确的5年数据
    # yfinance end date is exclusive, so add 1 day to current date to include today's data if available
    end_date_fetch = datetime.now(TIMEZONE).date() + timedelta(days=1)
    # Get enough data for 5 years back, plus some buffer for initial calculations like 200MA
    start_date_fetch = end_date_fetch - timedelta(days=5 * 365 + 10) # 稍微多取几天，确保覆盖5年和指标计算
    df_5y = _fetch_historical_df(ticker, start=start_date_fetch, end=end_date_fetch, interval='1d')
    
    if df_5y.empty:
        logger.warning("未找到 %s 的 5 年历史数据，跳过保存。", ticker)
        return ""

    return _save_historical_data_to_storage(ticker, df_5y)


def daily_incremental_update_job(ticker: str) -> None:
    """
    对单个股票的历史数据执行每日增量更新，补齐到最新交易日期。
    如果现有数据存在断档，将尝试补全。
    """
    logger.info("处理 %s 的每日增量更新...", ticker)
    existing_df = _load_historical_data_from_storage(ticker)
    
    today_date_naive = datetime.now(TIMEZONE).date() # 获取当前时区的 naive 日期
    fetch_start_date = None

    if existing_df.empty:
        logger.info("未找到 %s 的历史数据，将尝试进行 5 年铺底。", ticker)
        # 如果没有历史数据，调用铺底函数
        backfill_5_year_data_job(ticker)
        existing_df = _load_historical_data_from_storage(ticker) # 重新加载铺底后的数据
        if existing_df.empty:
            logger.warning("尝试铺底后仍未找到 %s 的历史数据，无法进行增量更新。", ticker)
            raise ValueError(f"无法为 {ticker} 获取或铺底历史数据。")
        
        # 铺底后，起始日期是现有数据的最大日期+1天
        fetch_start_date = existing_df.index.max().date() + timedelta(days=1)
        logger.info("铺底后，将从 %s 开始增量更新。", fetch_start_date)
    else:
        # 从现有数据的最新日期开始向前回看一段时间，避免漏掉数据修订（Yahoo 会回填/更正最近几天）。
        last_existing_date = existing_df.index.max().date()
        refresh_days = 7
        fetch_start_date = max(existing_df.index.min().date(), last_existing_date - timedelta(days=refresh_days))
        logger.info(
            "现有数据最新日期为 %s，将从 %s 开始增量刷新（回看 %d 天以吸收数据修订）。",
            last_existing_date,
            fetch_start_date,
            refresh_days,
        )

    # 如果开始获取日期已经大于今天，则无需更新
    if fetch_start_date > today_date_naive:
        logger.info("%s 的历史数据已是最新，无需增量更新。", ticker)
        return

    # 获取从 fetch_start_date 到今天的所有数据
    # yfinance.history 的 end 参数是排他性的，所以这里需要 +1 天以包含今天
    fetch_end_date = today_date_naive + timedelta(days=1) 
    # _fetch_historical_df 现在返回 timezone-naive DatetimeIndex，与 existing_df 兼容
    latest_df = _fetch_historical_df(ticker, start=fetch_start_date, end=fetch_end_date, interval='1d') 

    if latest_df.empty:
        logger.warning("无法获取 %s 的最新日数据 (从 %s 到 %s)，跳过增量更新。", ticker, fetch_start_date, today_date_naive)
        return
    # --- 关键修改点优化：确保所有索引都只保留日期部分（截断时间） ---
    # 这会强制将所有 DatetimeIndex 的时间部分设为 00:00:00，确保在合并和去重时完全一致
    if not existing_df.empty:
        existing_df.index = existing_df.index.normalize() # normalize() 会将时间部分设为 00:00:00
    
    latest_df.index = latest_df.index.normalize() # 对新获取的数据也进行同样处理
    # --- 关键修改点 ---
    # 合并现有数据和新获取的数据
    combined_df = pd.concat([existing_df, latest_df])
    
    # 按日期（索引）排序，确保后续去重时 'last' 总是指最新获取的数据
    # 这一点很重要，以处理同一天数据可能被多次更新的情况
    combined_df = combined_df.sort_index()
    
    # 根据索引（日期）进行去重，保留每个日期的最后一条记录
    # 这里 ~combined_df.index.duplicated(keep='last') 意味着筛选出索引不重复的行，
    # 或者对于重复的索引，保留最后出现的那个
    merged_df = combined_df[~combined_df.index.duplicated(keep='last')]

    _save_historical_data_to_storage(ticker, merged_df)
    logger.info("完成 %s 的每日增量更新。总记录数: %d", ticker, len(merged_df))


def _generate_ticker_ai_context(ticker: str) -> Dict:
    """
    生成指定股票的 AI Context，包括特征、最近一个月的每日数据和最近一年的每月 K 线数据。
    假定 GCS 中有 5 年的历史数据。
    """
    ticker = ticker.upper()
    logger.info("为 %s 生成 AI Context...", ticker)

    # 1. 加载完整的历史数据（5 年）
    # _load_historical_data_from_storage 确保返回 timezone-naive DatetimeIndex
    df_full = _load_historical_data_from_storage(ticker)
    if df_full.empty:
        logger.warning("未找到 %s 的历史数据以生成 AI Context。", ticker)
        return {"ticker": ticker, "status": "failed", "reason": "No historical data available."}

    if not isinstance(df_full.index, pd.DatetimeIndex):
        logger.error("df_full 的索引不是 DatetimeIndex，无法为 %s 生成 AI Context。", ticker)
        return {"ticker": ticker, "status": "failed", "reason": "Historical data index format error."}
    
    # 确保索引是排好序的
    df_full = df_full.sort_index()

    # 2. 计算特征（使用完整数据进行稳健计算，如 200 日均线）
    features = _compute_features(df_full) # _compute_features现在内部处理索引
    if not features:
        logger.warning("未能为 %s 计算全面的特征，AI Context 可能不完整。", ticker)
        # 即使特征不完整也继续，但标记问题
        pass

    # --- 修改：格式化 features 字典中的浮点数，保留两位小数 ---
    for key, value in features.items():
        if isinstance(value, float):
            features[key] = round(value, 6)
    
    # 3. 提取最近一个月的每日数据（大约 30 个交易日）
    end_date_for_slice = datetime.now(TIMEZONE).date()
    start_date_month_ago = end_date_for_slice - timedelta(days=30)
    
    # 筛选数据，索引已经是 naive DatetimeIndex，直接比较 date 属性即可
    last_month_daily_df = df_full[df_full.index.date >= start_date_month_ago].copy()

    # --- 修改：格式化每日数据中的价格列，保留两位小数 ---
    price_cols_to_round = ['Open', 'High', 'Low', 'Close']
    for col in price_cols_to_round:
        if col in last_month_daily_df.columns:
            last_month_daily_df[col] = pd.to_numeric(last_month_daily_df[col], errors='coerce').round(2)

    # 转换为 dict 列表以进行 JSON 序列化，处理日期格式和 NaNs
    last_month_daily_data = last_month_daily_df.reset_index()
    last_month_daily_data['Date'] = last_month_daily_data['Date'].dt.strftime("%Y-%m-%d")
    last_month_daily_data = last_month_daily_data.replace({np.nan: None}).to_dict(orient="records")
    
    # 4. 生成最近一年的月 K 线数据
    # _resample_to_monthly 返回带有 DatetimeIndex (naive) 的 DataFrame
    monthly_df_full = _resample_to_monthly(df_full)
    
    # 过滤最近 12 个月（大约 365 天）
    start_date_year_ago = end_date_for_slice - timedelta(days=365)
    last_year_monthly_df = monthly_df_full[monthly_df_full.index.date >= start_date_year_ago].reset_index()

    # --- 修改：格式化每月数据中的价格列，保留两位小数 ---
    for col in price_cols_to_round:
        if col in last_year_monthly_df.columns:
            last_year_monthly_df[col] = pd.to_numeric(last_year_monthly_df[col], errors='coerce').round(2)
    
    # 转换为 dict 列表以进行 JSON 序列化，处理日期格式和 NaNs
    last_year_monthly_data = last_year_monthly_df.copy()
    last_year_monthly_data['Date'] = last_year_monthly_data['Date'].dt.strftime("%Y-%m-%d")
    last_year_monthly_data = last_year_monthly_data.replace({np.nan: None}).to_dict(orient="records")

    ai_context = {
        "ticker": ticker,
        "last_updated": datetime.now(TIMEZONE).isoformat(),
        "features": features,
        "last_month_daily_data": last_month_daily_data,
        "last_year_monthly_data": last_year_monthly_data,
    }
    
    logger.info("成功为 %s 生成 AI Context。", ticker)
    return ai_context


def _process_ticker_for_batch(ticker: str, current_date: date) -> Dict[str, str]:
    """
    辅助函数，处理单个股票的批量操作：
    每日历史数据更新（包括自动铺底或补全），AI Context 生成和保存，以及每日索引更新。
    """
    ticker = ticker.upper()
    try:
        # 1. 每日增量更新历史数据（包含自动铺底或补全逻辑）
        # 此函数内部会处理：如果没有历史数据则铺底，如果存在断档则补全
        daily_incremental_update_job(ticker) 

        # 2. 生成 AI Context
        ai_context_data = _generate_ticker_ai_context(ticker)
        if ai_context_data.get("status") == "failed":
            return {"status": "failed", "message": ai_context_data.get("reason", "AI Context 生成失败。")}

        # 3. 保存 AI Context 作为 .txt 文件
        gcs_ai_context_path = _save_ai_context_to_storage(ticker, current_date, ai_context_data)
        if not gcs_ai_context_path:
            return {"status": "failed", "message": "保存 AI Context 到存储失败。"}

        # 4. 更新每日 AI Context 索引
        daily_index = _load_daily_index_from_storage(current_date)
        # 移除该股票的现有条目（用于幂等性）
        daily_index = [item for item in daily_index if item.get("ticker") != ticker]
        daily_index.append({"ticker": ticker, "path": gcs_ai_context_path})
        _save_daily_index_to_storage(current_date, daily_index)
        
        analysis_path = ""
        if GENERATE_ANALYSIS_IN_BATCH:
            try:
                df_for_analysis = _load_historical_data_from_storage(ticker)
                if not df_for_analysis.empty:
                    # Years is mostly meta for now; keep it stable for MVP.
                    analysis_payload = _compute_analysis_from_df(
                        symbol=ticker,
                        df=df_for_analysis,
                        years=5,
                        weights_override=None,
                        user_factor=None,
                    )
                    analysis_path = _save_analysis_to_storage(ticker, current_date, analysis_payload)
                    if analysis_path:
                        analysis_index = _load_analysis_daily_index_from_storage(current_date)
                        analysis_index = [item for item in analysis_index if item.get("ticker") != ticker]
                        analysis_index.append({"ticker": ticker, "path": analysis_path})
                        _save_analysis_daily_index_to_storage(current_date, analysis_index)
            except Exception as exc:
                # Analysis precompute should not fail the whole batch; it can be computed on-demand later.
                logger.warning("为 %s 预计算 analysis 失败（将跳过）: %s", ticker, exc)

        logger.info("成功为 %s 完成批量处理。AI Context 路径: %s, Analysis 路径: %s", ticker, gcs_ai_context_path, analysis_path or "N/A")
        result: Dict[str, str] = {"status": "success", "filepath": gcs_ai_context_path}
        if analysis_path:
            result["analysis_path"] = analysis_path
        return result

    except Exception as exc:
        logger.error("为 %s 执行批量处理时出错: %s", ticker, exc)
        return {"status": "failed", "message": f"批量处理时异常: {str(exc)}"}


# --- Dynamic Ticker List Management ---

def _load_dynamic_ticker_list() -> List[str]:
    """
    从 GCS 加载动态股票列表。如果失败或不存在，则回退到硬编码的默认列表。
    """
    if not GCS_BUCKET_NAME:
        logger.warning("GCS_BUCKET_NAME 未设置，使用硬编码的默认股票列表作为 fallback。")
        return _DEFAULT_HARDCODED_TICKERS

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_TICKER_LIST_BLOB_NAME)

        if not blob.exists():
            logger.warning("在 GCS 中未找到股票列表文件: gs://%s/%s。将使用硬编码的默认列表。", GCS_BUCKET_NAME, GCS_TICKER_LIST_BLOB_NAME)
            # 如果文件不存在，自动创建并上传硬编码的默认列表
            _save_dynamic_ticker_list(_DEFAULT_HARDCODED_TICKERS)
            return _DEFAULT_HARDCODED_TICKERS
        
        json_content = blob.download_as_text(encoding="utf-8")
        ticker_list = json.loads(json_content)
        if not isinstance(ticker_list, list) or not all(isinstance(t, str) for t in ticker_list):
            logger.error("GCS 中的股票列表文件格式无效，应为字符串列表。将使用硬编码的默认列表。")
            return _DEFAULT_HARDCODED_TICKERS
        
        logger.info("成功从 GCS 加载动态股票列表: gs://%s/%s", GCS_BUCKET_NAME, GCS_TICKER_LIST_BLOB_NAME)
        return ticker_list

    except Exception as exc:
        logger.error("从 GCS 加载动态股票列表失败: %s。将使用硬编码的默认列表。", exc)
        return _DEFAULT_HARDCODED_TICKERS

def _save_dynamic_ticker_list(ticker_list: List[str]) -> str:
    """
    将股票列表保存到 GCS。
    """
    if not GCS_BUCKET_NAME:
        logger.warning("GCS_BUCKET_NAME 未设置，无法保存动态股票列表到 GCS。")
        return ""

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_TICKER_LIST_BLOB_NAME)
        
        json_content = json.dumps(ticker_list, indent=2, ensure_ascii=False)
        blob.upload_from_string(json_content, content_type="application/json")
        logger.info("成功保存动态股票列表到 GCS: gs://%s/%s", GCS_BUCKET_NAME, GCS_TICKER_LIST_BLOB_NAME)
        return f"gs://{GCS_BUCKET_NAME}/{GCS_TICKER_LIST_BLOB_NAME}"
    except Exception as exc:
        logger.error("保存动态股票列表到 GCS 失败: %s", exc)
        return ""


# --- FastAPI Application Lifecycle Events ---

@app.on_event("startup")
async def startup_event():
    global scheduler, _CURRENT_ACTIVE_TICKERS
    logger.info("交易数据服务启动事件触发。")
    
    # 1. 加载动态股票列表
    _CURRENT_ACTIVE_TICKERS = _load_dynamic_ticker_list()
    if not _CURRENT_ACTIVE_TICKERS:
        logger.warning("动态股票列表为空或加载失败，请通过 /admin/update_default_tickers 接口配置或检查 GCS_BUCKET_NAME。")
        # 即使列表为空，也让服务启动，但数据处理部分可能不会执行

    # 2. 移除或注释掉启动时自动执行数据处理的代码块，避免多实例不同步竞争
    # 2. 容器启动时，为当前激活的股票列表执行铺底/增量补全历史数据，并生成AI Context
    # logger.info("检查并为当前激活的股票执行初始数据铺底、增量补全及 AI Context 生成...")
    # current_processing_date = datetime.now(TIMEZONE).date()

    # if not is_trading_day(current_processing_date):
    #     logger.info("%s 不是交易日（周末），跳过启动时的自动数据处理。", current_processing_date)
    #     return

    # for ticker in _CURRENT_ACTIVE_TICKERS:
    #     logger.info("启动时处理股票: %s", ticker)
    #     # _process_ticker_for_batch 内部会处理历史数据的铺底/增量补全和AI Context的生成/保存
    #     result = _process_ticker_for_batch(ticker, current_processing_date)
    #     if result.get("status") == "success":
    #         logger.info("启动时为 %s 成功完成数据处理。路径: %s", ticker, result.get("filepath"))
    #     else:
    #         logger.error("启动时为 %s 处理数据失败: %s", ticker, result.get("message"))

    logger.info("交易数据服务启动完成。实时数据将按需获取。")


@app.on_event("shutdown")
async def shutdown_event():
    # global scheduler
    # if scheduler:
    #     scheduler.shutdown()
    #     logger.info("交易数据服务 BackgroundScheduler 关闭。")
    logger.info("应用程序关闭事件触发。")


# --- API Routes ---

@app.get("/trading_data/{ticker}", summary="获取实时行情数据")
async def get_trading_data_endpoint(ticker: str) -> Dict:
    """
    **主要行为:** 获取指定股票的实时行情数据。

    **关键行动:**
    *   **获取数据:** ✅ 会。实时从 `yfinance` 获取指定股票的最新行情信息。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ❌ 不会。（直接返回数据内容）

    **用途:** 适用于需要获取指定股票最新、最原始的实时行情数据的场景，不涉及数据持久化或 AI Context 生成。
    """
    ticker = ticker.upper()
    request_logger.info(f"API Request: /trading_data/{ticker} - Real-time quote")
    quote_data = _fetch_quote(ticker)
    if not quote_data or not quote_data.get("price"):
        raise HTTPException(status_code=404, detail=f"实时数据 {ticker} 未找到或无法获取。")
    return quote_data


@app.get("/trading_data/{ticker}/historical", summary="获取历史行情数据")
async def get_historical_data_endpoint(ticker: str, period: str = Query('1y', pattern="^(3y|1y|3mo|5y)$")) -> List[Dict]:
    """
    **主要行为:** 获取指定股票的历史 K 线数据。

    **关键行动:**
    *   **获取数据:** ✅ 会。实时从 `yfinance` 获取指定周期（'3y', '1y', '3mo', '5y'）的历史日 K 线数据。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ❌ 不会。（直接返回数据内容）

    **用途:** 适用于需要获取指定股票最新、最原始的历史 K 线数据的场景，不涉及数据持久化或 AI Context 生成。
    """
    ticker = ticker.upper()
    request_logger.info(f"API Request: /trading_data/{ticker}/historical - Period: {period}")

    period_days = 365
    if period == "3mo":
        period_days = 92
    elif period == "1y":
        period_days = 365
    elif period == "3y":
        period_days = 3 * 365
    elif period == "5y":
        period_days = 5 * 365

    # Prefer persisted data (GCS) when available; refresh only when stale (rate-limited).
    df = _load_historical_data_from_storage(ticker)
    try:
        if not df.empty and isinstance(df.index, pd.DatetimeIndex):
            df = df.sort_index()
            last_date = df.index.max().date()
            expected = _latest_expected_trading_day(datetime.now(TIMEZONE).date())
            if last_date < expected:
                _maybe_refresh_daily_once(ticker, min_interval_seconds=600)
                df = _load_historical_data_from_storage(ticker).sort_index()
    except Exception:
        pass

    if df.empty:
        # Fallback: fetch via yfinance, and persist so subsequent requests are fast + consistent.
        df = _fetch_historical_df(ticker, period=period)  # Returns with DatetimeIndex (naive)
        if df.empty:
            raise HTTPException(status_code=404, detail=f"周期 {period} 的 {ticker} 历史数据未找到。")
        if isinstance(df.index, pd.DatetimeIndex):
            df.index = df.index.normalize()
        _save_historical_data_to_storage(ticker, df)

    # Slice to requested period (approx) to reduce payload.
    if isinstance(df.index, pd.DatetimeIndex) and len(df.index):
        cutoff = datetime.now(TIMEZONE).date() - timedelta(days=int(period_days))
        df = df[df.index.date >= cutoff].copy()
        if df.empty:
            raise HTTPException(status_code=404, detail=f"周期 {period} 的 {ticker} 历史数据未找到。")

    # 转换为 dict 列表以便 API 响应
    # 确保 'Date' 列转换为字符串，并将 NaN 替换以便 JSON 序列化
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()
        df['Date'] = df['Date'].dt.strftime("%Y-%m-%d")
    df = df.replace({np.nan: None}) # 替换数据中的 NaNs
    
    return df.to_dict(orient="records")


@app.get("/portfolio/prices", summary="获取财富自由组合的最新收盘价")
async def get_portfolio_prices_endpoint(tickers: str = Query(..., description="逗号分隔的资产代码")) -> Dict[str, Dict[str, float]]:
    """返回每日批量生成的组合行情（美元）。"""
    if not tickers or not tickers.strip():
        raise HTTPException(status_code=400, detail="tickers 参数不能为空。")

    requested = [ticker.strip() for ticker in tickers.split(",") if ticker.strip()]
    if not requested:
        raise HTTPException(status_code=400, detail="tickers 参数不能为空。")

    request_logger.info(f"API Request: /portfolio/prices - tickers={requested}")

    payload: Dict[str, Dict[str, float]] = {}
    missing: List[str] = []

    for raw_ticker in requested:
        upper = raw_ticker.upper()

        if upper in _STATIC_PORTFOLIO_TICKERS:
            payload[raw_ticker] = {"price": 1.0, "changePercent": 0.0}
            continue

        resolved = _resolve_portfolio_ticker(raw_ticker)
        if not resolved:
            missing.append(raw_ticker)
            continue

        snapshot = _load_latest_price_snapshot(resolved)
        if snapshot:
            payload[raw_ticker] = snapshot
        else:
            missing.append(raw_ticker)

    if missing:
        logger.warning("以下 ticker 缺少批量行情数据: %s", ",".join(missing))

    return payload


@app.get("/trading_data/{ticker}/features", summary="获取行情特征数据")
async def get_features_endpoint(ticker: str, period: str = Query('1y', pattern="^(3y|1y|3mo|5y)$")) -> Dict:
    """
    **主要行为:** 获取指定股票的交易特征数据（技术指标、信号等）。

    **关键行动:**
    *   **获取数据:** ✅ 会。实时从 `yfinance` 获取原始数据，并计算包括 RSI、MACD、移动平均线、趋势和买卖信号等特征。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ❌ 不会。（直接返回数据内容）

    **用途:** 适用于需要获取指定股票最新、最原始的交易特征数据的场景，不涉及数据持久化或 AI Context 生成。
    """
    ticker = ticker.upper()
    request_logger.info(f"API Request: /trading_data/{ticker}/features - Period: {period}")
    df = _fetch_historical_df(ticker, period=period) # Returns with DatetimeIndex (naive)
    features = _compute_features(df)
    if not features:
        raise HTTPException(status_code=404, detail=f"未能计算 {ticker} 周期 {period} 的特征。数据可能不足。")
    return features


@app.post("/trading_data/{ticker}/backfill_5y", summary="手动触发获取并保存5年铺底交易数据到GCS")
async def manual_backfill_5y_data(ticker: str) -> Dict[str, str]:
    """
    **主要行为:** 手动触发获取指定股票的5年历史 K 线数据并保存到 GCS。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 `yfinance` 获取5年历史数据。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。数据将持久化到 GCS 的 `historical_data/` 目录下。
    *   **提供路径:** ✅ 会。（返回保存后的 GCS 路径）

    **用途:** 适用于首次部署或数据丢失后，对单个股票进行历史数据铺底的场景。此操作耗时较长，建议谨慎手动触发。
    """
    ticker = ticker.upper()
    request_logger.info(f"API Request: /trading_data/{ticker}/backfill_5y - Manual 5-year backfill")
    filepath = backfill_5_year_data_job(ticker)
    if not filepath:
        raise HTTPException(status_code=500, detail=f"未能为 {ticker} 铺底 5 年数据。请检查服务日志。")
    return {"ticker": ticker, "message": f"成功启动 5 年数据铺底。数据已保存至 {filepath}。"}


@app.post("/trading_data/daily_update_all_historical", summary="【Cloud Scheduler调用】手动触发所有默认股票的日增量历史数据更新到GCS")
async def daily_update_all_historical_trigger() -> Dict[str, Any]:
    """
    **主要行为:** 为所有默认关注的股票执行每日增量历史数据更新（仅历史数据，不生成AI Context）。此函数会检查并自动补全历史数据。

    **关键行动:**
    *   **获取数据:** ✅ 会。加载 GCS 中已有的历史数据，并从 `yfinance` 获取最新日 K 线数据，进行合并和去重。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。更新后的历史数据将持久化到 GCS 的 `historical_data/` 目录下。
    *   **提供路径:** ❌ 不会。（返回处理状态，不返回文件路径）

    **用途:** 如果不需要同时生成AI Context，可由 Google Cloud Scheduler 等外部调度器每日定时调用，确保历史数据是最新的。
    """
    request_logger.info(f"API Request: /trading_data/daily_update_all_historical - Triggered by external scheduler (or manually).")
    today_date = datetime.now(TIMEZONE).date()
    if not is_trading_day(today_date):
        logger.info("%s 不是交易日（周末），跳过每日更新。", today_date)
        return {"message": f"{today_date} 不是交易日（周末），跳过每日更新。"}

    results: Dict[str, str] = {}
    for ticker in _CURRENT_ACTIVE_TICKERS: # 使用动态列表
        try:
            daily_incremental_update_job(ticker) # 内部会处理铺底和增量补全
            results[ticker] = "success"
        except Exception as exc:
            logger.error("为 %s 执行每日更新时出错: %s", ticker, exc)
            results[ticker] = f"failed: {str(exc)}"
    logger.info("每日历史数据更新完成。")
    return {"message": "每日历史数据更新已触发。", "results": results}


@app.post("/trading_data/batch_process_all", summary="【Cloud Scheduler调用】全量铺底并生成所有股票的批量增量更新")
async def batch_process_all_trigger() -> Dict[str, Any]:
    """
    **主要行为:** 执行所有当前激活股票的完整每日批量处理，包括：
    1.  每日增量更新历史交易数据（自动判断是否铺底及补全）。
    2.  生成并保存每支股票的 AI 分析上下文数据。
    3.  更新当日 AI Context 清单。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 `yfinance` 和 GCS 加载数据。
    *   **生成 AI Context:** ✅ 会。
    *   **保存 JSON:** ✅ 会。历史数据保存到 `historical_data/`，AI Context 保存到 `ai_context/{TICKER}/{YYYY-MM-DD}.txt`。
    *   **提供路径:** ✅ 会。（返回每支股票 AI Context 生成结果及存储路径）

    **用途:** 作为核心每日任务，由 Cloud Scheduler 等外部调度器定时调用，确保所有相关数据（历史 K 线和 AI Context）保持最新。
    """
    request_logger.info(f"API Request: /trading_data/batch_process_all - Triggered by external scheduler (or manually).")
    current_processing_date = datetime.now(TIMEZONE).date() # 使用配置时区中的当前日期
    
    if not is_trading_day(current_processing_date):
        logger.info("%s 不是交易日，补上缺口交易数据。", current_processing_date)
    else:
        logger.info("%s 是交易日，执行批量处理。", current_processing_date)

    results: Dict[str, Dict] = {}
    for ticker in _CURRENT_ACTIVE_TICKERS: # 使用动态列表
        results[ticker] = _process_ticker_for_batch(ticker, current_processing_date)
    
    logger.info("所有股票的批量处理完成。")
    return {"message": "所有当前激活股票的批量处理已触发。", "results": results}


@app.post("/trading_data/batch_refresh", summary="批量刷新指定股票列表")
async def batch_refresh_specified_tickers(tickers: List[str] = Body(..., embed=True)) -> Dict[str, Any]:
    """
    **主要行为:** 批量处理指定股票的交易数据，包括：
    1.  对指定股票进行历史交易数据的增量更新（自动判断是否铺底及补全）。
    2.  生成并保存指定股票的 AI 分析上下文数据。
    3.  更新当日 AI Context 清单。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 `yfinance` 和 GCS 加载数据。
    *   **生成 AI Context:** ✅ 会。
    *   **保存 JSON:** ✅ 会。历史数据保存到 `historical_data/`，AI Context 保存到 `ai_context/{TICKER}/{YYYY-MM-DD}.txt`。
    *   **提供路径:** ✅ 会。（返回每支股票 AI Context 生成结果及存储路径）

    **用途:** 适用于需要按需刷新特定股票数据和 AI Context 的场景，例如在系统检测到重要事件发生时。
    """
    request_logger.info(f"API Request: /trading_data/batch_refresh - Triggered for tickers: {tickers}")
    current_processing_date = datetime.now(TIMEZONE).date() # 使用配置时区中的当前日期

    # if not is_trading_day(current_processing_date):
    #     logger.info("%s 不是交易日（周末），跳过批量刷新。", current_processing_date)
    #     return {"message": f"{current_processing_date} 不是交易日（周末），跳过指定股票的批量刷新。"}
    
    if not is_trading_day(current_processing_date):
        logger.info("%s 不是交易日，补上缺口交易数据。暂时每天都跑，保证图卡正常展示", current_processing_date)
    else:
        logger.info("%s 是交易日，执行批量处理。", current_processing_date)

    results: Dict[str, Dict] = {}
    for ticker in tickers:
        results[ticker] = _process_ticker_for_batch(ticker, current_processing_date)
    
    logger.info("指定股票的批量已完成。")
    return {"message": "指定股票的批量已完成。", "results": results}


# --- Admin API for Ticker List Management ---
@app.post("/admin/update_default_tickers", summary="更新当前激活的股票列表")
async def update_default_tickers(new_tickers: List[str] = Body(..., description="要设置为默认列表的新股票代码列表。")) -> Dict[str, Any]:
    """
    **主要行为:** 更新服务使用的默认股票代码列表，并将其持久化到 GCS。

    **关键行动:**
    *   **获取数据:** ❌ 不会。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ✅ 会。新的股票列表将保存到 GCS 中的 `config/default_tickers.json`。
    *   **提供路径:** ✅ 会。（返回保存后的 GCS 路径）

    **用途:** 允许运维人员或管理员动态修改服务关注的股票列表，无需重新部署代码。
    **注意:** 更新操作仅影响当前实例和未来启动的新实例。已运行的其他实例会继续使用其启动时加载的列表，直到它们被回收或重启。
    """
    global _CURRENT_ACTIVE_TICKERS
    request_logger.info(f"API Request: /admin/update_default_tickers - Received new tickers: {new_tickers}")
    
    if not all(isinstance(t, str) and t.strip() for t in new_tickers):
        raise HTTPException(status_code=400, detail="请求体必须是有效的字符串股票代码列表。")

    # 更新内存中的列表
    _CURRENT_ACTIVE_TICKERS = [t.upper() for t in new_tickers]
    
    # 保存到 GCS
    filepath = _save_dynamic_ticker_list(_CURRENT_ACTIVE_TICKERS)
    
    if not filepath:
        raise HTTPException(status_code=500, detail="未能将新的股票列表保存到 GCS。")
    
    return {"message": "成功更新股票列表。", "new_active_tickers": _CURRENT_ACTIVE_TICKERS, "saved_to": filepath}


@app.post("/admin/load_ticker_list_from_gcs", summary="从 GCS 重新加载当前激活股票列表")
async def load_ticker_list_from_gcs() -> Dict[str, Any]:
    """重新从 GCS 加载 `config/default_tickers.json` 到内存，不修改远端文件。

    使用场景:
    - 其他实例 / 运维已通过接口或直接编辑 GCS 文件更新了列表，本实例无需重启即可同步。

    返回内容包括：新列表、原列表与差异（新增 / 移除）。
    """
    global _CURRENT_ACTIVE_TICKERS
    request_logger.info("API Request: /admin/load_ticker_list_from_gcs - Reloading ticker list from GCS")

    old_list = list(_CURRENT_ACTIVE_TICKERS) if _CURRENT_ACTIVE_TICKERS else []
    new_list = _load_dynamic_ticker_list()
    # 统一大写 & 去除空白
    _CURRENT_ACTIVE_TICKERS = [t.upper().strip() for t in new_list if isinstance(t, str) and t.strip()]

    old_set = set(old_list)
    new_set = set(_CURRENT_ACTIVE_TICKERS)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)

    logger.info(
        "重新加载股票列表完成。总数: %d (新增: %d, 移除: %d)",
        len(_CURRENT_ACTIVE_TICKERS), len(added), len(removed)
    )

    return {
        "message": "已从 GCS 重新加载股票列表。",
        "count": len(_CURRENT_ACTIVE_TICKERS),
        "active_tickers": _CURRENT_ACTIVE_TICKERS,
        "added": added,
        "removed": removed,
        "previous_count": len(old_list),
    }


@app.get("/ai_context/daily_index", summary="获取指定日期的AI Context清单")
async def get_daily_ai_context_index(date: str = Query(..., description="日期格式: YYYY-MM-DD")):
    """
    **主要行为:** 获取指定日期所有已生成 AI Context 文件的清单。

    **关键行动:**
    *   **获取数据:** ✅ 会。从 GCS 加载当日的 AI Context 索引文件。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ✅ 会。（返回一个列表，包含每个股票的股票代码和对应的 AI Context 文件在 GCS 上的路径）

    **用途:** 适用于图卡服务或批量处理任务，通过一次调用获取当日所有更新过的 AI Context 文件的 GCS 路径，避免自行构造。
    """
    request_logger.info(f"API Request: /ai_context/daily_index - Date: {date}")
    try:
        index_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式无效。请使用 YYYY-MM-DD。")
    
    daily_index = _load_daily_index_from_storage(index_date)
    return { "date": index_date, "items": daily_index }


@app.get("/ai_context/{ticker}/by_date/{date}", summary="获取某支股票某日的AI Context路径")
async def get_ai_context_path_by_date(ticker: str, date: str):
    """
    **主要行为:** 获取指定股票在特定日期生成的 AI Context 文件在 GCS 上的路径。

    **关键行动:**
    *   **获取数据:** ❌ 不会。（不加载实际文件内容）
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON：** ❌ 不会。
    *   **提供路径:** ✅ 仅根据提供的股票代码和日期构造并返回对应的 AI Context 文件在 GCS 上的路径（`gs://<bucket_name>/ai_context/{TICKER}/{YYYY-MM-DD}.json`）。

    **用途:** 适用于 QA 问答系统或需要单个股票特定日期 AI Context 文件路径的场景。
    """
    request_logger.info(f"API Request: /ai_context/{ticker}/by_date/{date}")
    ticker = ticker.upper()
    try:
        context_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式无效。请使用 YYYY-MM-DD。")
    
    # Construct the path based on GCS_BUCKET_NAME existence
    if GCS_BUCKET_NAME:
        gcs_path = f"gs://{GCS_BUCKET_NAME}/{_get_gcs_ai_context_blob_name(ticker, context_date)}"
        return {"ticker": ticker, "date": date, "path": gcs_path}
    else:
        local_path = _get_local_fallback_ai_context_filepath(ticker, context_date)
        return {"ticker": ticker, "date": date, "path": local_path}


# --- Health check endpoint ---

@app.get("/health", summary="健康检查")
async def health() -> Dict[str, str]:
    """
    **主要行为:** 提供一个简单的健康检查接口。

    **关键行动:**
    *   **获取数据:** ❌ 不会。
    *   **生成 AI Context:** ❌ 不会。
    *   **保存 JSON:** ❌ 不会。
    *   **提供路径:** ❌ 不会。

    **用途:** 用于 Cloud Run 或其他容器编排平台进行服务健康检查。
    """
    return {
        "status": "ok",
        "gcs_enabled": "1" if bool(GCS_BUCKET_NAME) else "0",
        "gcs_bucket": str(GCS_BUCKET_NAME or ""),
        "generate_analysis_in_batch": "1" if GENERATE_ANALYSIS_IN_BATCH else "0",
    }


# --- Compatibility endpoints for vercel-nextjs (optional, but helps swap data source later) ---

@app.get("/api/market/candles", summary="(compat) 获取市场K线数据")
async def market_candles(
    symbol: str = Query(..., description="股票代码，例如 AAPL"),
    interval: str = Query("1d", description="仅支持 1d"),
    limit: int = Query(780, ge=30, le=2000, description="最多返回多少根K线"),
    range: Optional[str] = Query(None, description="1y/2y/5y/10y（可选，用于兜底拉取）"),
):
    """
    返回格式对齐 `vercel-nextjs/app/api/market/candles`：candles=[{t,o,h,l,c,v}]
    """
    s = _normalize_symbol(symbol)
    if interval != "1d":
        raise HTTPException(status_code=400, detail="Only interval=1d is supported for now.")

    df = _load_historical_data_from_storage(s)
    source = "gcs" if GCS_BUCKET_NAME else "local-fallback"

    # If we have stored data but it's stale, refresh it (rate-limited per instance).
    try:
        if not df.empty and isinstance(df.index, pd.DatetimeIndex):
            last_date = df.index.max().date()
            expected = _latest_expected_trading_day(datetime.now(TIMEZONE).date())
            if last_date < expected:
                _maybe_refresh_daily_once(s, min_interval_seconds=600)
                df = _load_historical_data_from_storage(s)
    except Exception:
        pass

    # Fallback: if no stored data, fetch via yfinance and persist.
    if df.empty:
        years_guess = 5
        if isinstance(range, str) and range.strip():
            r = range.strip().lower()
            years_guess = 1 if r == "1y" else 2 if r == "2y" else 5 if r == "5y" else 10 if r == "10y" else 5
        df = _fetch_historical_df(s, period=_years_to_range(years_guess), interval="1d")
        if df.empty:
            raise HTTPException(status_code=502, detail=f"Failed to load candles for {s}.")
        df.index = df.index.normalize()
        _save_historical_data_to_storage(s, df)
        source = "yfinance"

    candles = _df_to_market_candles(df, limit=limit)
    if len(candles) < 30:
        raise HTTPException(status_code=502, detail="Not enough candles.")

    df = df.sort_index()
    return {
        "symbol": s,
        "interval": "1d",
        "candles": candles,
        "source": source,
        "range": range or "stored",
    }


@app.post("/api/trading/us/analysis", summary="(compat) 计算日线分析输出（概率/权重/因子）")
async def trading_us_analysis(body: Dict[str, Any] = Body(default_factory=dict)):
    """
    请求/返回尽量对齐 `vercel-nextjs/app/api/trading/us/analysis`.
    目前为 MVP：不做回测训练，只做可解释的因子打分 + sigmoid 融合。
    """
    symbol = _normalize_symbol(body.get("symbol"))
    years = _clamp_int(body.get("years"), 5, 1, 10)
    # horizonDays/backtestWindow 先接受但不使用（留给订阅后升级）。
    _ = _clamp_int(body.get("horizonDays"), 1, 1, 10)
    _ = _clamp_int(body.get("backtestWindow"), 252, 20, 756)

    # Ensure stored candles are up-to-date enough (best-effort).
    try:
        _maybe_refresh_daily_once(symbol, min_interval_seconds=600)
    except Exception:
        # ignore for MVP (still try to compute from stored data / fresh fetch)
        pass

    df = _load_historical_data_from_storage(symbol)
    if df.empty:
        df = _fetch_historical_df(symbol, period=_years_to_range(years), interval="1d")
        if df.empty:
            raise HTTPException(status_code=502, detail=f"Failed to load candles for {symbol}.")
        df.index = df.index.normalize()
        _save_historical_data_to_storage(symbol, df)

    try:
        df = df.sort_index()
        analysis_date = df.index.max().date() if isinstance(df.index, pd.DatetimeIndex) and len(df.index) else datetime.now(TIMEZONE).date()

        is_baseline = _analysis_request_is_baseline(body)
        inflight_key = f"{symbol}:{analysis_date.isoformat()}:{years}"

        if is_baseline:
            cached = _load_analysis_from_storage(symbol, analysis_date)
            if isinstance(cached, dict):
                # Keep response meta fresh without mutating the cached object too much.
                meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else {}
                meta = {**meta, "servedFrom": "gcs-cache", "servedAt": datetime.now(TIMEZONE).isoformat(), "years": years}
                cached["meta"] = meta
                return JSONResponse(
                    content=cached,
                    headers={"cache-control": "public, max-age=60, stale-while-revalidate=3600"},
                )

            with _ANALYSIS_LOCK:
                ev = _ANALYSIS_INFLIGHT.get(inflight_key)
                if ev is None:
                    ev = Event()
                    _ANALYSIS_INFLIGHT[inflight_key] = ev
                    leader = True
                else:
                    leader = False

            if not leader:
                # Wait for the leader to finish computing & persisting, then try load again.
                ev.wait(timeout=12.0)
                cached2 = _load_analysis_from_storage(symbol, analysis_date)
                if isinstance(cached2, dict):
                    meta = cached2.get("meta") if isinstance(cached2.get("meta"), dict) else {}
                    meta = {**meta, "servedFrom": "gcs-cache", "servedAt": datetime.now(TIMEZONE).isoformat(), "years": years}
                    cached2["meta"] = meta
                    return JSONResponse(
                        content=cached2,
                        headers={"cache-control": "public, max-age=60, stale-while-revalidate=3600"},
                    )

        analysis_payload = _compute_analysis_from_df(
            symbol=symbol,
            df=df,
            years=years,
            weights_override=body.get("weights") if isinstance(body.get("weights"), dict) else None,
            user_factor=body.get("userFactor") if isinstance(body.get("userFactor"), dict) else None,
        )
        if is_baseline:
            path = _try_save_analysis_if_absent(symbol, analysis_date, analysis_payload)
            if path:
                try:
                    index_list = _load_analysis_daily_index_from_storage(analysis_date)
                    index_list = [item for item in index_list if item.get("ticker") != symbol]
                    index_list.append({"ticker": symbol, "path": path})
                    _save_analysis_daily_index_to_storage(analysis_date, index_list)
                except Exception:
                    pass

        return JSONResponse(
            content=analysis_payload,
            headers={"cache-control": "public, max-age=60, stale-while-revalidate=3600"},
        )
    except Exception as exc:
        if _analysis_request_is_baseline(body):
            with _ANALYSIS_LOCK:
                ev = _ANALYSIS_INFLIGHT.pop(inflight_key, None) if "inflight_key" in locals() else None
            if ev is not None:
                ev.set()
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        if _analysis_request_is_baseline(body):
            with _ANALYSIS_LOCK:
                ev = _ANALYSIS_INFLIGHT.pop(inflight_key, None) if "inflight_key" in locals() else None
            if ev is not None:
                ev.set()


if __name__ == "__main__": # pragma: no cover
    import uvicorn
    # When running locally without GCS_BUCKET_NAME env var, it will use local fallback for historical data
    # and print to console.
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
