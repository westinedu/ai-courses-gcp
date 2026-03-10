from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from google.cloud import storage


@dataclass(frozen=True)
class OptionsSummaryDeps:
    gcs_bucket_name: Optional[str]
    timezone: Any
    market_close_grace_minutes: int
    now_in_market_timezone: Callable[[], datetime]
    market_close_at: Callable[[datetime], datetime]
    is_trading_day: Callable[[date], bool]
    is_during_regular_market_session: Callable[[datetime], bool]
    is_after_market_close: Callable[[datetime], bool]
    normalize_symbol: Callable[[Any], str]
    request_logger: logging.Logger


class OptionsSummaryService:
    def __init__(self, deps: OptionsSummaryDeps, local_fallback_options_dir: Optional[str] = None):
        self._deps = deps
        self._lock = Lock()
        self._inflight: Dict[str, Event] = {}
        self._l1_cache: Dict[str, Dict[str, Any]] = {}

        self._session_max_age_seconds = int(os.environ.get("OPTIONS_SESSION_MAX_AGE_SECONDS", "600"))  # 10 min
        self._offsession_max_age_seconds = int(os.environ.get("OPTIONS_OFFSESSION_MAX_AGE_SECONDS", "86400"))  # 24 h
        self._miss_max_age_seconds = int(os.environ.get("OPTIONS_MISS_MAX_AGE_SECONDS", "21600"))  # 6 h

        fallback_dir = (
            local_fallback_options_dir
            if local_fallback_options_dir
            else os.path.join(os.path.dirname(os.path.dirname(__file__)), "options_local_fallback")
        )
        self._local_fallback_options_dir = os.path.abspath(fallback_dir)
        if not self._deps.gcs_bucket_name:
            os.makedirs(self._local_fallback_options_dir, exist_ok=True)

    @property
    def request_logger(self) -> logging.Logger:
        return self._deps.request_logger

    def normalize_symbol(self, raw: Any) -> str:
        return self._deps.normalize_symbol(raw)

    def get_or_refresh_summary(self, ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
        t = ticker.upper()

        if not force_refresh:
            l1 = self._get_from_l1(t)
            if isinstance(l1, dict) and self._is_payload_fresh(l1):
                return self._with_meta(l1, cache_layer="l1")

        with self._lock:
            wait_ev = self._inflight.get(t)
            if wait_ev is None:
                wait_ev = Event()
                self._inflight[t] = wait_ev
                leader = True
            else:
                leader = False

        if not leader:
            wait_ev.wait(timeout=15.0)
            l1_after_wait = self._get_from_l1(t)
            if isinstance(l1_after_wait, dict):
                if self._is_payload_fresh(l1_after_wait):
                    return self._with_meta(l1_after_wait, cache_layer="l1-after-wait")
                return self._with_meta(l1_after_wait, cache_layer="l1-after-wait-stale", stale=True, stale_reason="expired")
            l2_after_wait = self._load_from_storage(t)
            if isinstance(l2_after_wait, dict):
                self._set_to_l1(t, l2_after_wait)
                if self._is_payload_fresh(l2_after_wait):
                    return self._with_meta(l2_after_wait, cache_layer="l2-after-wait")
                return self._with_meta(l2_after_wait, cache_layer="l2-after-wait-stale", stale=True, stale_reason="expired")
            raise RuntimeError(f"等待中的 options 请求未产生可用结果: {t}")

        try:
            if not force_refresh:
                l2 = self._load_from_storage(t)
                if isinstance(l2, dict) and self._is_payload_fresh(l2):
                    self._set_to_l1(t, l2)
                    return self._with_meta(l2, cache_layer="l2")

            payload = self._fetch_from_yfinance(t)
            self._save_to_storage(t, payload)
            self._set_to_l1(t, payload)
            return self._with_meta(payload, cache_layer="upstream")
        except Exception as exc:
            stale_payload = self._load_from_storage(t)
            if isinstance(stale_payload, dict):
                self._set_to_l1(t, stale_payload)
                return self._with_meta(
                    stale_payload,
                    cache_layer="l2-stale",
                    stale=True,
                    stale_reason=str(exc),
                )
            raise
        finally:
            with self._lock:
                done = self._inflight.pop(t, None)
            if done is not None:
                done.set()

    def _get_gcs_blob_name(self, ticker: str) -> str:
        return f"options/summary/{ticker.upper()}.json"

    def _get_local_fallback_filepath(self, ticker: str) -> str:
        os.makedirs(self._local_fallback_options_dir, exist_ok=True)
        return os.path.join(self._local_fallback_options_dir, f"{ticker.upper()}.json")

    def _load_from_storage(self, ticker: str) -> Optional[Dict[str, Any]]:
        t = ticker.upper()
        gcs_bucket_name = self._deps.gcs_bucket_name
        if gcs_bucket_name:
            try:
                storage_client = storage.Client()
                bucket = storage_client.bucket(gcs_bucket_name)
                blob_name = self._get_gcs_blob_name(t)
                blob = bucket.blob(blob_name)
                if not blob.exists():
                    return None
                raw = blob.download_as_text(encoding="utf-8")
                payload = json.loads(raw)
                return payload if isinstance(payload, dict) else None
            except Exception as exc:
                logging.getLogger(__name__).error("从 GCS 加载 %s 的 options 摘要失败: %s", t, exc)
                return None

        filepath = self._get_local_fallback_filepath(t)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logging.getLogger(__name__).error("从本地 fallback 文件 %s 加载 %s 的 options 摘要失败: %s", filepath, t, exc)
            return None

    def _save_to_storage(self, ticker: str, payload: Dict[str, Any]) -> str:
        t = ticker.upper()
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        gcs_bucket_name = self._deps.gcs_bucket_name

        if gcs_bucket_name:
            try:
                storage_client = storage.Client()
                bucket = storage_client.bucket(gcs_bucket_name)
                blob_name = self._get_gcs_blob_name(t)
                blob = bucket.blob(blob_name)
                blob.cache_control = "public, max-age=120, stale-while-revalidate=3600"
                blob.upload_from_string(content, content_type="application/json")
                logging.getLogger(__name__).info("成功保存 %s 的 options 摘要到 GCS: gs://%s/%s", t, gcs_bucket_name, blob_name)
                return f"gs://{gcs_bucket_name}/{blob_name}"
            except Exception as exc:
                logging.getLogger(__name__).error("保存 %s 的 options 摘要到 GCS 失败: %s", t, exc)
                return ""

        filepath = self._get_local_fallback_filepath(t)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logging.getLogger(__name__).warning("GCS_BUCKET_NAME 未设置。已保存 %s 的 options 摘要到本地 fallback: %s", t, filepath)
            return filepath
        except Exception as exc:
            logging.getLogger(__name__).error("保存 %s 的 options 摘要到本地 fallback 文件 %s 失败: %s", t, filepath, exc)
            return ""

    @staticmethod
    def _as_finite_float(value: Any) -> Optional[float]:
        try:
            n = float(value)
        except Exception:
            return None
        return n if np.isfinite(n) else None

    @staticmethod
    def _safe_ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
        if num is None or den is None or den == 0:
            return None
        out = num / den
        return out if np.isfinite(out) else None

    @staticmethod
    def _parse_expiration_date(raw: Any) -> Optional[date]:
        s = str(raw or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    @staticmethod
    def _series_sum(values: Any) -> float:
        if values is None:
            return 0.0
        try:
            s = pd.to_numeric(values, errors="coerce").dropna()
            return float(s.sum()) if len(s) else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _series_mean(values: Any) -> Optional[float]:
        if values is None:
            return None
        try:
            s = pd.to_numeric(values, errors="coerce").dropna()
            if len(s) == 0:
                return None
            out = float(s.mean())
            return out if np.isfinite(out) else None
        except Exception:
            return None

    @staticmethod
    def _series_weighted_mean(values: Any, weights: Any) -> Optional[float]:
        if values is None or weights is None:
            return None
        try:
            v = pd.to_numeric(values, errors="coerce")
            w = pd.to_numeric(weights, errors="coerce")
            mask = v.notna() & w.notna() & (w > 0)
            if not bool(mask.any()):
                return None
            vv = v[mask].astype(float).to_numpy()
            ww = w[mask].astype(float).to_numpy()
            den = float(ww.sum())
            if den <= 0:
                return None
            out = float((vv * ww).sum() / den)
            return out if np.isfinite(out) else None
        except Exception:
            return None

    def _nearest_strike(self, strikes: List[float], target_price: Optional[float]) -> Optional[float]:
        clean = [float(x) for x in strikes if self._as_finite_float(x) is not None]
        if not clean:
            return None
        if target_price is None or target_price <= 0:
            clean.sort()
            return clean[len(clean) // 2]
        return min(clean, key=lambda x: abs(x - float(target_price)))

    @staticmethod
    def _nearest_option_row(df: pd.DataFrame, strike: Optional[float]) -> Optional[pd.Series]:
        if strike is None or not isinstance(df, pd.DataFrame) or df.empty or "strike" not in df.columns:
            return None
        try:
            strikes = pd.to_numeric(df["strike"], errors="coerce")
            if not bool(strikes.notna().any()):
                return None
            idx = (strikes - float(strike)).abs().idxmin()
            row = df.loc[idx]
            if isinstance(row, pd.DataFrame):
                if row.empty:
                    return None
                return row.iloc[0]
            return row if isinstance(row, pd.Series) else None
        except Exception:
            return None

    def _pick_option_price_from_row(self, row: Optional[pd.Series]) -> Optional[float]:
        if row is None:
            return None
        last_price = self._as_finite_float(row.get("lastPrice"))
        if last_price is not None and last_price >= 0:
            return last_price
        bid = self._as_finite_float(row.get("bid"))
        ask = self._as_finite_float(row.get("ask"))
        if bid is not None and ask is not None and bid >= 0 and ask >= 0:
            mid = (bid + ask) / 2.0
            return mid if np.isfinite(mid) else None
        return self._as_finite_float(row.get("mark"))

    def _resolve_underlying_price_from_yf(self, stock: yf.Ticker, ticker: str) -> Optional[float]:
        try:
            fast_info = stock.fast_info
            if fast_info is not None:
                for key in ("lastPrice", "regularMarketPrice", "previousClose"):
                    price = self._as_finite_float(fast_info.get(key))
                    if price is not None and price > 0:
                        return price
        except Exception:
            pass

        try:
            info = stock.info or {}
            for key in ("regularMarketPrice", "currentPrice", "previousClose"):
                price = self._as_finite_float(info.get(key))
                if price is not None and price > 0:
                    return price
        except Exception:
            pass

        try:
            hist = stock.history(period="5d", interval="1d")
            if isinstance(hist, pd.DataFrame) and not hist.empty and "Close" in hist.columns:
                close = self._as_finite_float(hist["Close"].dropna().iloc[-1])
                if close is not None and close > 0:
                    return close
        except Exception as exc:
            logging.getLogger(__name__).warning("获取 %s 标的价格失败（options fallback）: %s", ticker, exc)

        return None

    @staticmethod
    def _round_or_none(value: Optional[float], digits: int) -> Optional[float]:
        if value is None or not np.isfinite(value):
            return None
        return round(float(value), int(digits))

    def _fetch_from_yfinance(self, ticker: str) -> Dict[str, Any]:
        t = ticker.upper()
        now_market = self._deps.now_in_market_timezone()
        market_date = now_market.date()
        fetched_at = datetime.now(self._deps.timezone).isoformat()

        stock = yf.Ticker(t)
        expirations_raw: List[str] = []
        try:
            expirations_raw = list(stock.options or [])
        except Exception as exc:
            raise RuntimeError(f"加载 {t} 期权到期日失败: {exc}")

        expirations: List[date] = []
        for item in expirations_raw:
            d = self._parse_expiration_date(item)
            if d is not None:
                expirations.append(d)
        expirations = sorted(set(expirations))

        if not expirations:
            return {
                "symbol": t,
                "provider": "yfinance",
                "source": "option_chain",
                "fetchedAt": fetched_at,
                "marketDate": market_date.isoformat(),
                "hasOptions": False,
                "expirationCount": 0,
                "nearestExpiration": None,
                "dte": None,
                "underlyingPrice": self._round_or_none(self._resolve_underlying_price_from_yf(stock, t), 4),
                "metrics": {},
                "chain": {"calls": 0, "puts": 0},
            }

        nearest_exp = next((d for d in expirations if d >= market_date), expirations[0])
        nearest_exp_str = nearest_exp.isoformat()

        try:
            option_chain = stock.option_chain(nearest_exp_str)
            calls = option_chain.calls if isinstance(option_chain.calls, pd.DataFrame) else pd.DataFrame()
            puts = option_chain.puts if isinstance(option_chain.puts, pd.DataFrame) else pd.DataFrame()
        except Exception as exc:
            raise RuntimeError(f"加载 {t} 期权链失败({nearest_exp_str}): {exc}")

        underlying_price = self._resolve_underlying_price_from_yf(stock, t)
        call_volume = self._series_sum(calls["volume"]) if "volume" in calls.columns else 0.0
        put_volume = self._series_sum(puts["volume"]) if "volume" in puts.columns else 0.0
        call_oi = self._series_sum(calls["openInterest"]) if "openInterest" in calls.columns else 0.0
        put_oi = self._series_sum(puts["openInterest"]) if "openInterest" in puts.columns else 0.0

        put_call_volume_ratio = self._safe_ratio(put_volume, call_volume)
        put_call_oi_ratio = self._safe_ratio(put_oi, call_oi)

        call_iv_weighted = (
            self._series_weighted_mean(calls.get("impliedVolatility"), calls.get("openInterest"))
            if "impliedVolatility" in calls.columns
            else None
        )
        put_iv_weighted = (
            self._series_weighted_mean(puts.get("impliedVolatility"), puts.get("openInterest"))
            if "impliedVolatility" in puts.columns
            else None
        )
        call_iv_mean = call_iv_weighted if call_iv_weighted is not None else self._series_mean(calls.get("impliedVolatility"))
        put_iv_mean = put_iv_weighted if put_iv_weighted is not None else self._series_mean(puts.get("impliedVolatility"))

        strike_candidates: List[float] = []
        if "strike" in calls.columns:
            strike_candidates.extend([float(x) for x in pd.to_numeric(calls["strike"], errors="coerce").dropna().tolist()])
        if "strike" in puts.columns:
            strike_candidates.extend([float(x) for x in pd.to_numeric(puts["strike"], errors="coerce").dropna().tolist()])
        atm_strike = self._nearest_strike(strike_candidates, underlying_price)

        atm_call_row = self._nearest_option_row(calls, atm_strike)
        atm_put_row = self._nearest_option_row(puts, atm_strike)
        atm_call_iv = self._as_finite_float(atm_call_row.get("impliedVolatility")) if atm_call_row is not None else None
        atm_put_iv = self._as_finite_float(atm_put_row.get("impliedVolatility")) if atm_put_row is not None else None
        atm_iv_values = [x for x in (atm_call_iv, atm_put_iv) if x is not None and np.isfinite(x)]
        atm_iv = (sum(atm_iv_values) / len(atm_iv_values)) if atm_iv_values else None

        atm_call_price = self._pick_option_price_from_row(atm_call_row)
        atm_put_price = self._pick_option_price_from_row(atm_put_row)
        atm_straddle = (
            (atm_call_price + atm_put_price)
            if atm_call_price is not None and atm_put_price is not None
            else None
        )
        implied_move_pct = (
            (atm_straddle / underlying_price)
            if atm_straddle is not None and underlying_price is not None and underlying_price > 0
            else None
        )

        return {
            "symbol": t,
            "provider": "yfinance",
            "source": "option_chain",
            "fetchedAt": fetched_at,
            "marketDate": market_date.isoformat(),
            "hasOptions": True,
            "expirationCount": len(expirations),
            "expirationsPreview": [d.isoformat() for d in expirations[:6]],
            "nearestExpiration": nearest_exp_str,
            "dte": int((nearest_exp - market_date).days),
            "underlyingPrice": self._round_or_none(underlying_price, 4),
            "metrics": {
                "putCallVolumeRatio": self._round_or_none(put_call_volume_ratio, 4),
                "putCallOpenInterestRatio": self._round_or_none(put_call_oi_ratio, 4),
                "callVolume": int(round(call_volume)),
                "putVolume": int(round(put_volume)),
                "callOpenInterest": int(round(call_oi)),
                "putOpenInterest": int(round(put_oi)),
                "callIvMean": self._round_or_none(call_iv_mean, 6),
                "putIvMean": self._round_or_none(put_iv_mean, 6),
                "atmStrike": self._round_or_none(atm_strike, 4),
                "atmCallIv": self._round_or_none(atm_call_iv, 6),
                "atmPutIv": self._round_or_none(atm_put_iv, 6),
                "atmIv": self._round_or_none(atm_iv, 6),
                "atmCallPrice": self._round_or_none(atm_call_price, 6),
                "atmPutPrice": self._round_or_none(atm_put_price, 6),
                "atmStraddle": self._round_or_none(atm_straddle, 6),
                "impliedMovePct": self._round_or_none(implied_move_pct, 6),
            },
            "chain": {"calls": int(len(calls)), "puts": int(len(puts))},
        }

    def _parse_iso_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self._deps.timezone)
            return dt
        except Exception:
            return None

    def _is_payload_fresh(self, payload: Dict[str, Any]) -> bool:
        fetched_at = self._parse_iso_datetime(payload.get("fetchedAt"))
        if fetched_at is None:
            return False

        now_market = self._deps.now_in_market_timezone()
        fetched_utc = fetched_at.astimezone(pytz.UTC)
        age_seconds = (datetime.now(pytz.UTC) - fetched_utc).total_seconds()
        in_session_ttl = float(max(60, self._session_max_age_seconds))
        has_options = bool(payload.get("hasOptions"))
        off_session_ttl = float(max(in_session_ttl, self._offsession_max_age_seconds if has_options else self._miss_max_age_seconds))

        if self._deps.is_during_regular_market_session(now_market):
            return age_seconds <= in_session_ttl

        if self._deps.is_trading_day(now_market.date()) and self._deps.is_after_market_close(now_market):
            payload_market_date = str(payload.get("marketDate") or "").strip()
            today_market = now_market.date().isoformat()
            if payload_market_date != today_market:
                return False

            close_gate_market = self._deps.market_close_at(now_market) + timedelta(minutes=self._deps.market_close_grace_minutes)
            close_gate_utc = close_gate_market.astimezone(pytz.UTC)
            if fetched_utc < close_gate_utc:
                return False
            return age_seconds <= off_session_ttl

        return age_seconds <= off_session_ttl

    def _l1_ttl_seconds(self, payload: Dict[str, Any]) -> float:
        now_market = self._deps.now_in_market_timezone()
        if self._deps.is_during_regular_market_session(now_market):
            return float(max(60, self._session_max_age_seconds))
        has_options = bool(payload.get("hasOptions"))
        return float(max(300, self._offsession_max_age_seconds if has_options else self._miss_max_age_seconds))

    def _get_from_l1(self, ticker: str) -> Optional[Dict[str, Any]]:
        t = ticker.upper()
        now = time.time()
        with self._lock:
            entry = self._l1_cache.get(t)
            if not entry:
                return None
            if now >= float(entry.get("expiresAt", 0)):
                self._l1_cache.pop(t, None)
                return None
            payload = entry.get("payload")
            return dict(payload) if isinstance(payload, dict) else None

    def _set_to_l1(self, ticker: str, payload: Dict[str, Any]) -> None:
        t = ticker.upper()
        ttl = self._l1_ttl_seconds(payload)
        with self._lock:
            self._l1_cache[t] = {
                "payload": dict(payload),
                "expiresAt": time.time() + ttl,
            }

    @staticmethod
    def _with_meta(payload: Dict[str, Any], cache_layer: str, stale: bool = False, stale_reason: str = "") -> Dict[str, Any]:
        out = dict(payload)
        out["cacheLayer"] = cache_layer
        if stale:
            out["stale"] = True
            if stale_reason:
                out["staleReason"] = stale_reason
        return out


def build_options_router(service: OptionsSummaryService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/market/options/summary", summary="(compat) 获取期权指标摘要")
    async def market_options_summary(
        symbol: str = Query(..., description="股票代码，例如 AAPL"),
        force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新上游"),
    ):
        """
        返回最近可用到期日的期权链摘要指标（PCR/ATM IV/隐含波动幅度等）。
        缓存策略:
        - 盘中: 10 分钟级刷新
        - 收盘后: 若缓存仍是收盘前快照，请求到达时会强制刷新一次
        - 非交易时段: 长 TTL，避免周末高频拉取
        """
        s = service.normalize_symbol(symbol)
        service.request_logger.info(f"API Request: /api/market/options/summary - symbol={s}, force_refresh={force_refresh}")
        try:
            payload = service.get_or_refresh_summary(s, force_refresh=bool(force_refresh))
            return JSONResponse(
                content=payload,
                headers={"cache-control": "public, max-age=60, stale-while-revalidate=600"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to load options summary for {s}: {str(exc)}")

    @router.get("/trading_data/{ticker}/options/summary", summary="获取期权指标摘要（trading_data 路径）")
    async def trading_data_options_summary(
        ticker: str,
        force_refresh: int = Query(0, ge=0, le=1, description="是否强制绕过缓存刷新上游"),
    ):
        t = service.normalize_symbol(ticker)
        return await market_options_summary(symbol=t, force_refresh=force_refresh)

    return router

