from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yfinance as yf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("heatmap_service")

app = FastAPI(
    title="Heatmap Service",
    description="Multi-market heatmap snapshot service (HK first, extensible to KS/US/CN/JP).",
    version="0.1.0",
)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "heatmap-service").strip() or "heatmap-service"
CACHE_TTL_SECONDS = max(5, int(os.environ.get("HEATMAP_CACHE_TTL_SECONDS", "300")))
GCS_BUCKET_NAME = (os.environ.get("GCS_BUCKET_NAME") or "").strip()
GCS_PREFIX = (os.environ.get("HEATMAP_GCS_PREFIX") or "heatmap/snapshots").strip().strip("/")
MARKETS_CONFIG_BLOB = (os.environ.get("HEATMAP_MARKETS_CONFIG_BLOB") or "").strip()
WRITE_HISTORY = str(os.environ.get("HEATMAP_WRITE_HISTORY", "0")).strip().lower() in {"1", "true", "yes"}
CRON_TOKEN = (os.environ.get("HEATMAP_CRON_TOKEN") or "").strip()
DEFAULT_MARKET = (os.environ.get("HEATMAP_DEFAULT_MARKET") or "hk").strip().lower() or "hk"
# Soft timeout for one market refresh. 0 means no soft timeout.
QUOTE_TIMEOUT_SECONDS = max(0, int(os.environ.get("HEATMAP_QUOTE_TIMEOUT_SECONDS", "180")))
MAX_FAILED_COUNT = max(0, int(os.environ.get("HEATMAP_MAX_FAILED_COUNT", "3")))
MAX_FAILED_RATIO = max(0.0, min(1.0, float(os.environ.get("HEATMAP_MAX_FAILED_RATIO", "0.03"))))

LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "markets.json"

_storage_client: Optional[storage.Client] = None
_config_lock = threading.Lock()
_snapshot_cache: Dict[str, Dict[str, Any]] = {}
_snapshot_locks: Dict[str, threading.Lock] = {}
markets_config: Dict[str, Any] = {}

SCHEDULER_TIMEZONE = ZoneInfo("Asia/Hong_Kong")

MARKET_REFRESH_POLICIES: Dict[str, Dict[str, Any]] = {
    "hk": {
        "cadence_seconds": 10 * 60,
        "phase_minute": 0,
        "window_start_minute": 9 * 60 + 30,
        "window_end_minute": 16 * 60 + 10,
        "weekdays": {0, 1, 2, 3, 4},
    },
    "tw": {
        "cadence_seconds": 10 * 60,
        "phase_minute": 0,
        "window_start_minute": 9 * 60,
        # TWSE closes at 13:30. Keep one post-close forced slot so delayed
        # quote vendors can publish the official close before the last snapshot.
        "window_end_minute": 13 * 60 + 40,
        "weekdays": {0, 1, 2, 3, 4},
        "forced_slot_minutes": {13 * 60 + 40},
    },
    "jp": {
        "cadence_seconds": 30 * 60,
        "phase_minute": 20,
        "window_start_minute": 8 * 60,
        "window_end_minute": 14 * 60 + 30,
        "weekdays": {0, 1, 2, 3, 4},
        "slot_refresh": True,
    },
    "ks": {
        "cadence_seconds": 30 * 60,
        "phase_minute": 0,
        "window_start_minute": 8 * 60 + 20,
        "window_end_minute": 14 * 60 + 30,
        "weekdays": {0, 1, 2, 3, 4},
        "slot_refresh": True,
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_to_iso(value: Any) -> Optional[str]:
    seconds = _safe_float(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        n = float(value)
    except Exception:
        return None
    if n != n:
        return None
    return n


def _safe_int(value: Any) -> Optional[int]:
    n = _safe_float(value)
    if n is None:
        return None
    if not float(n).is_integer():
        return None
    return int(n)


def _allowed_failed_count(requested_count: int) -> int:
    if requested_count <= 0:
        return 0
    return max(MAX_FAILED_COUNT, math.ceil(requested_count * MAX_FAILED_RATIO))


def _snapshot_completeness_issue(snapshot: Any, market: Optional[str] = None) -> Optional[str]:
    if not isinstance(snapshot, dict):
        return "snapshot is not a JSON object"

    expected_market = (market or "").strip().lower()
    snapshot_market = str(snapshot.get("market") or "").strip().lower()
    if expected_market and snapshot_market != expected_market:
        return f"market mismatch ({snapshot_market or '<missing>'} != {expected_market})"

    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return "nodes missing"

    count = _safe_int(snapshot.get("count"))
    if count is None:
        return "count missing"
    if count != len(nodes):
        return f"count mismatch ({count} != {len(nodes)})"

    requested_count = _safe_int(snapshot.get("requestedCount"))
    if requested_count is None or requested_count <= 0:
        return "requestedCount missing"

    failed_count = _safe_int(snapshot.get("failedCount"))
    if failed_count is None or failed_count < 0:
        return "failedCount missing"

    failed_tickers = snapshot.get("failedTickers")
    if _coerce_bool(snapshot.get("timedOut")):
        return "timedOut=true"
    if count + failed_count != requested_count:
        return f"accounting mismatch ({count}+{failed_count}!={requested_count})"
    if isinstance(failed_tickers, list) and len(failed_tickers) != failed_count:
        return f"failedTickers mismatch ({len(failed_tickers)} != {failed_count})"
    if failed_count > _allowed_failed_count(requested_count):
        return f"failedCount={failed_count} exceeds allowance"

    return None


def _snapshot_is_complete(snapshot: Any, market: Optional[str] = None) -> bool:
    return _snapshot_completeness_issue(snapshot, market) is None


def _read_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Config file {path} must be a JSON object.")
    return payload


def _storage() -> Optional[storage.Client]:
    global _storage_client
    if not GCS_BUCKET_NAME:
        return None
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _load_markets_config_from_gcs() -> Optional[Dict[str, Any]]:
    client = _storage()
    if client is None or not MARKETS_CONFIG_BLOB:
        return None
    try:
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(MARKETS_CONFIG_BLOB)
        if not blob.exists(client=client):
            logger.warning("GCS config blob not found: gs://%s/%s", GCS_BUCKET_NAME, MARKETS_CONFIG_BLOB)
            return None
        raw = blob.download_as_text()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            logger.warning("GCS markets config is not a JSON object: gs://%s/%s", GCS_BUCKET_NAME, MARKETS_CONFIG_BLOB)
            return None
        logger.info("Loaded markets config from GCS: gs://%s/%s", GCS_BUCKET_NAME, MARKETS_CONFIG_BLOB)
        return payload
    except Exception as exc:
        logger.exception("Failed to load markets config from GCS: %s", exc)
        return None


def _load_markets_config() -> Dict[str, Any]:
    local_payload = _read_json_file(LOCAL_CONFIG_PATH)
    remote_payload = _load_markets_config_from_gcs()
    payload = dict(local_payload)
    canonical_keys = {str(key).strip().lower(): key for key in payload.keys()}
    if isinstance(remote_payload, dict):
        for raw_key, cfg in remote_payload.items():
            lookup_key = str(raw_key).strip().lower()
            target_key = canonical_keys.get(lookup_key, raw_key)
            if isinstance(cfg, dict):
                base_cfg = payload.get(target_key)
                merged_cfg = dict(base_cfg) if isinstance(base_cfg, dict) else {}
                merged_cfg.update(cfg)
                payload[target_key] = merged_cfg
            else:
                payload[target_key] = cfg
            canonical_keys[lookup_key] = target_key

    normalized: Dict[str, Any] = {}
    for raw_key, cfg in payload.items():
        key = str(raw_key).strip().lower()
        if not key or not isinstance(cfg, dict):
            continue
        constituents = cfg.get("constituents")
        if not isinstance(constituents, list) or not constituents:
            continue

        clean_constituents: List[Dict[str, str]] = []
        for row in constituents:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            clean_constituents.append(
                {
                    "ticker": ticker,
                    "name": str(row.get("name") or ticker).strip() or ticker,
                    "sector": str(row.get("sector") or "Other").strip() or "Other",
                }
            )

        if not clean_constituents:
            continue

        normalized[key] = {
            "code": key,
            "name": str(cfg.get("name") or key.upper()).strip() or key.upper(),
            "timezone": str(cfg.get("timezone") or "UTC").strip() or "UTC",
            "currency": str(cfg.get("currency") or "").strip().upper() or None,
            "description": str(cfg.get("description") or "").strip(),
            "index": None,
            "constituents": clean_constituents,
        }

        raw_index = cfg.get("index")
        if isinstance(raw_index, dict):
            index_ticker = str(raw_index.get("ticker") or "").strip().upper()
            if index_ticker:
                normalized[key]["index"] = {
                    "ticker": index_ticker,
                    "name": str(raw_index.get("name") or index_ticker).strip() or index_ticker,
                }

    if not normalized:
        raise RuntimeError("No valid market config found for heatmap service.")
    return normalized


def _get_market_lock(market: str) -> threading.Lock:
    key = market.lower()
    with _config_lock:
        lock = _snapshot_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _snapshot_locks[key] = lock
        return lock


def _latest_blob_path(market: str) -> str:
    return f"{GCS_PREFIX}/{market}/latest.json"


def _history_blob_path(market: str, generated_at: str) -> str:
    suffix = generated_at.replace("-", "").replace(":", "").replace("+00:00", "Z").replace("T", "_")
    return f"{GCS_PREFIX}/{market}/history/{suffix}.json"


def _load_latest_complete_history_snapshot_from_gcs(
    market: str,
    *,
    bucket: Optional[storage.Bucket] = None,
    client: Optional[storage.Client] = None,
) -> Optional[Dict[str, Any]]:
    resolved_client = client or _storage()
    if resolved_client is None:
        return None
    resolved_bucket = bucket or resolved_client.bucket(GCS_BUCKET_NAME)
    history_prefix = f"{GCS_PREFIX}/{market}/history/"
    try:
        blobs = list(resolved_client.list_blobs(resolved_bucket, prefix=history_prefix))
    except Exception as exc:
        logger.exception("Failed listing GCS history snapshots for market=%s: %s", market, exc)
        return None

    for blob in sorted(blobs, key=lambda item: item.name, reverse=True):
        try:
            data = json.loads(blob.download_as_text())
        except Exception as exc:
            logger.warning("Failed reading history snapshot blob=%s: %s", blob.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        issue = _snapshot_completeness_issue(data, market)
        if issue is None:
            data["_from"] = "gcs-history"
            return data
        logger.warning("Ignoring incomplete history snapshot blob=%s market=%s: %s", blob.name, market, issue)
    return None


def _load_snapshot_from_gcs(market: str) -> Optional[Dict[str, Any]]:
    client = _storage()
    if client is None:
        return None
    path = _latest_blob_path(market)
    try:
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(path)
        if not blob.exists(client=client):
            return _load_latest_complete_history_snapshot_from_gcs(market, bucket=bucket, client=client)
        data = json.loads(blob.download_as_text())
        if not isinstance(data, dict):
            return _load_latest_complete_history_snapshot_from_gcs(market, bucket=bucket, client=client)
        issue = _snapshot_completeness_issue(data, market)
        if issue is not None:
            logger.warning("Ignoring incomplete GCS latest snapshot for market=%s: %s", market, issue)
            history_snapshot = _load_latest_complete_history_snapshot_from_gcs(market, bucket=bucket, client=client)
            if history_snapshot is not None:
                history_snapshot["stale"] = True
                history_snapshot["error"] = f"Latest snapshot incomplete and was skipped: {issue}"
                return history_snapshot
            return None
        data["_from"] = "gcs"
        return data
    except Exception as exc:
        logger.exception("Failed loading GCS snapshot (%s): %s", path, exc)
        return None


def _latest_snapshot(market: str) -> Optional[Dict[str, Any]]:
    entry = _snapshot_cache.get(market)
    snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
    if isinstance(snapshot, dict) and _snapshot_is_complete(snapshot, market):
        return snapshot
    if isinstance(snapshot, dict):
        issue = _snapshot_completeness_issue(snapshot, market) or "unknown issue"
        logger.warning("Ignoring incomplete memory snapshot for market=%s: %s", market, issue)
    return _load_snapshot_from_gcs(market)


def _refresh_policy_for_market(market: str) -> Dict[str, Any]:
    return MARKET_REFRESH_POLICIES.get(
        market,
        {
            "cadence_seconds": 10 * 60,
            "phase_minute": 0,
            "window_start_minute": 0,
            "window_end_minute": 24 * 60,
            "weekdays": {0, 1, 2, 3, 4},
        },
    )


def _current_slot_start(local_now: datetime, cadence_minutes: int, phase_minute: int) -> datetime:
    minute_of_day = local_now.hour * 60 + local_now.minute
    remainder = (minute_of_day - phase_minute) % cadence_minutes
    slot_minute = minute_of_day - remainder
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(minutes=slot_minute)


def _refresh_decision(market: str, *, force: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    policy = _refresh_policy_for_market(market)
    cadence_seconds = int(policy.get("cadence_seconds") or 0)
    phase_minute = int(policy.get("phase_minute") or 0)

    if force:
        return {
            "refresh": True,
            "reason": "force",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
        }

    local_now = (now or datetime.now(timezone.utc)).astimezone(SCHEDULER_TIMEZONE)
    minute_of_day = local_now.hour * 60 + local_now.minute
    slot_refresh = _coerce_bool(policy.get("slot_refresh"))
    weekdays = policy.get("weekdays") or {0, 1, 2, 3, 4}
    if local_now.weekday() not in weekdays:
        return {
            "refresh": False,
            "reason": "outside_weekday",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
        }

    window_start = int(policy.get("window_start_minute") or 0)
    window_end = int(policy.get("window_end_minute") or 24 * 60)
    if minute_of_day < window_start or minute_of_day > window_end:
        return {
            "refresh": False,
            "reason": "outside_window",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
        }

    latest_snapshot = _latest_snapshot(market)
    latest_generated_at = None
    if isinstance(latest_snapshot, dict):
        latest_generated_at = _parse_iso_datetime(latest_snapshot.get("generatedAt"))

    age_seconds = None
    if latest_generated_at is not None:
        age_seconds = max(0, int(((now or datetime.now(timezone.utc)) - latest_generated_at).total_seconds()))

    forced_slot_minutes = policy.get("forced_slot_minutes") or set()
    if minute_of_day in forced_slot_minutes:
        forced_slot_start_local = local_now.replace(second=0, microsecond=0)
        forced_slot_start_utc = forced_slot_start_local.astimezone(timezone.utc)
        if latest_generated_at is not None and latest_generated_at >= forced_slot_start_utc:
            return {
                "refresh": False,
                "reason": "forced_slot_already_refreshed",
                "cadenceSeconds": cadence_seconds,
                "phaseMinute": phase_minute,
                "ageSeconds": age_seconds,
            }
        return {
            "refresh": True,
            "reason": "forced_slot_due",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
            "ageSeconds": age_seconds,
        }

    if slot_refresh and cadence_seconds >= 60:
        cadence_minutes = max(1, cadence_seconds // 60)
        if minute_of_day % cadence_minutes != phase_minute % cadence_minutes:
            return {
                "refresh": False,
                "reason": "phase_not_due",
                "cadenceSeconds": cadence_seconds,
                "phaseMinute": phase_minute,
                "ageSeconds": age_seconds,
            }

        slot_start_local = _current_slot_start(local_now, cadence_minutes, phase_minute)
        slot_start_utc = slot_start_local.astimezone(timezone.utc)
        if latest_generated_at is not None and latest_generated_at >= slot_start_utc:
            return {
                "refresh": False,
                "reason": "slot_already_refreshed",
                "cadenceSeconds": cadence_seconds,
                "phaseMinute": phase_minute,
                "ageSeconds": age_seconds,
            }

        return {
            "refresh": True,
            "reason": "slot_due" if latest_generated_at is not None else "slot_cold_start",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
            "ageSeconds": age_seconds,
        }

    if latest_generated_at is None:
        return {
            "refresh": True,
            "reason": "cold_start",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
        }

    if age_seconds < cadence_seconds:
        return {
            "refresh": False,
            "reason": "cadence_not_due",
            "cadenceSeconds": cadence_seconds,
            "phaseMinute": phase_minute,
            "ageSeconds": age_seconds,
        }

    if cadence_seconds >= 60:
        cadence_minutes = max(1, cadence_seconds // 60)
        if minute_of_day % cadence_minutes != phase_minute % cadence_minutes:
            return {
                "refresh": False,
                "reason": "phase_not_due",
                "cadenceSeconds": cadence_seconds,
                "phaseMinute": phase_minute,
                "ageSeconds": age_seconds,
            }

    return {
        "refresh": True,
        "reason": "cadence_due",
        "cadenceSeconds": cadence_seconds,
        "phaseMinute": phase_minute,
        "ageSeconds": age_seconds,
    }


def _save_snapshot_to_gcs(market: str, snapshot: Dict[str, Any]) -> None:
    client = _storage()
    if client is None:
        return
    bucket = client.bucket(GCS_BUCKET_NAME)

    latest_path = _latest_blob_path(market)
    latest_blob = bucket.blob(latest_path)

    if WRITE_HISTORY:
        # Archive previous latest snapshot (if any) to history/<generatedAt>.json
        # before overwriting latest.json.
        try:
            if latest_blob.exists(client=client):
                previous_raw = latest_blob.download_as_text()
                previous_data = json.loads(previous_raw)
                if isinstance(previous_data, dict):
                    prev_generated_at = str(previous_data.get("generatedAt") or "").strip() or _utc_now_iso()
                    history_path = _history_blob_path(market, prev_generated_at)
                    history_blob = bucket.blob(history_path)
                    if not history_blob.exists(client=client):
                        history_blob.upload_from_string(previous_raw.encode("utf-8"), content_type="application/json")
        except Exception as exc:
            logger.warning("Failed archiving previous latest snapshot for market=%s: %s", market, exc)

    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    latest_blob.upload_from_string(body, content_type="application/json")


def _extract_fast_info(ticker_obj: Any) -> Dict[str, Any]:
    try:
        fast = ticker_obj.fast_info
        if isinstance(fast, dict):
            return fast
        return dict(fast)
    except Exception:
        return {}


def _pick_number(source: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        if key in source:
            n = _safe_float(source.get(key))
            if n is not None:
                return n
    return None


def _same_number(left: Optional[float], right: Optional[float], rel_tol: float = 1e-9, abs_tol: float = 1e-9) -> bool:
    if left is None or right is None:
        return False
    return math.isclose(float(left), float(right), rel_tol=rel_tol, abs_tol=abs_tol)


def _needs_prev_close_repair(price: Optional[float], prev_close: Optional[float]) -> bool:
    # Some APAC tickers return fast_info.regularMarketPreviousClose == lastPrice
    # after the session closes, which collapses heatmap colors to 0.00%.
    return price is not None and prev_close is not None and _same_number(price, prev_close)


def _fetch_one_quote(symbol: str, fallback_name: str, fallback_sector: str, fallback_currency: Optional[str]) -> Optional[Dict[str, Any]]:
    ticker_obj = yf.Ticker(symbol)
    fast = _extract_fast_info(ticker_obj)

    price = _pick_number(
        fast,
        [
            "lastPrice",
            "last_price",
            "regularMarketPrice",
            "currentPrice",
        ],
    )
    prev_close = _pick_number(
        fast,
        [
            "regularMarketPreviousClose",
            "regular_market_previous_close",
            "previousClose",
            "previous_close",
        ],
    )
    market_cap = _pick_number(fast, ["marketCap", "market_cap"])

    info: Dict[str, Any] = {}
    suspicious_prev_close = _needs_prev_close_repair(price, prev_close)
    if price is None or prev_close is None or market_cap is None or suspicious_prev_close:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}

    if price is None:
        price = _pick_number(info, ["regularMarketPrice", "currentPrice"])
    info_prev_close = _pick_number(info, ["regularMarketPreviousClose", "previousClose"])
    info_change_pct = _pick_number(info, ["regularMarketChangePercent"])
    if prev_close is None:
        prev_close = info_prev_close
    elif suspicious_prev_close and info_prev_close is not None and not _same_number(info_prev_close, price):
        prev_close = info_prev_close
    if market_cap is None:
        market_cap = _pick_number(info, ["marketCap"])

    history_price_used = False
    suspicious_prev_close = _needs_prev_close_repair(price, prev_close)
    if price is None or prev_close is None or suspicious_prev_close:
        try:
            hist = ticker_obj.history(period="5d", interval="1d", auto_adjust=False, actions=False)
            closes = []
            if hist is not None and not hist.empty and "Close" in hist.columns:
                closes = [
                    _safe_float(x) for x in hist["Close"].tolist() if _safe_float(x) is not None
                ]
            if price is None and closes:
                price = closes[-1]
                history_price_used = True
            if len(closes) >= 2:
                history_prev_close = closes[-2]
                if prev_close is None or (_needs_prev_close_repair(price, prev_close) and not _same_number(history_prev_close, price)):
                    prev_close = history_prev_close
            elif prev_close is None and len(closes) == 1:
                prev_close = closes[-1]
        except Exception:
            pass

    if price is None:
        return None

    change_pct = None
    if prev_close is not None and prev_close > 0:
        change_pct = ((price - prev_close) / prev_close) * 100.0
    if change_pct is None:
        change_pct = info_change_pct or 0.0

    currency = (
        str(fast.get("currency") or "").strip().upper()
        or str(info.get("currency") or "").strip().upper()
        or (fallback_currency or "")
        or None
    )

    name = (
        str(info.get("shortName") or "").strip()
        or str(info.get("longName") or "").strip()
        or fallback_name
        or symbol
    )

    # Treemap value should be positive and stable. Prefer market cap, then fallback synthetic.
    if market_cap is not None and market_cap > 0:
        value = market_cap
    else:
        value = max(price, 0.0) * 1_000_000.0

    point: Dict[str, Any] = {
        "ticker": symbol,
        "name": name,
        "sector": fallback_sector or "Other",
        "price": round(price, 4),
        "colorValue": round(float(change_pct), 4),
        "marketCap": round(float(market_cap), 2) if market_cap is not None and market_cap > 0 else None,
        "value": round(float(value), 2),
        "currency": currency,
        "source": "history" if history_price_used else "quote",
    }
    return point


def _positive_history_closes(history_frame: Any) -> List[float]:
    closes: List[float] = []
    if history_frame is None or getattr(history_frame, "empty", True) or "Close" not in history_frame.columns:
        return closes
    for raw in history_frame["Close"].tolist():
        close = _safe_float(raw)
        if close is not None and close > 0:
            closes.append(close)
    return closes


def _fetch_market_index(symbol: str, fallback_name: str, fallback_currency: Optional[str]) -> Optional[Dict[str, Any]]:
    ticker_obj = yf.Ticker(symbol)
    fast = _extract_fast_info(ticker_obj)

    price = _pick_number(
        fast,
        [
            "lastPrice",
            "last_price",
            "regularMarketPrice",
            "currentPrice",
        ],
    )
    prev_close = _pick_number(
        fast,
        [
            "regularMarketPreviousClose",
            "regular_market_previous_close",
            "previousClose",
            "previous_close",
        ],
    )

    info: Dict[str, Any] = {}
    suspicious_prev_close = _needs_prev_close_repair(price, prev_close)
    if price is None or prev_close is None or suspicious_prev_close:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}

    if price is None:
        price = _pick_number(info, ["regularMarketPrice", "currentPrice"])
    info_prev_close = _pick_number(info, ["regularMarketPreviousClose", "previousClose"])
    if prev_close is None:
        prev_close = info_prev_close
    elif suspicious_prev_close and info_prev_close is not None and not _same_number(info_prev_close, price):
        prev_close = info_prev_close

    history_price_used = False
    suspicious_prev_close = _needs_prev_close_repair(price, prev_close)
    if price is None or prev_close is None or suspicious_prev_close:
        try:
            hist = ticker_obj.history(period="5d", interval="1d", auto_adjust=False, actions=False)
            closes = _positive_history_closes(hist)
            if price is None and closes:
                price = closes[-1]
                history_price_used = True
            if len(closes) >= 2:
                history_prev_close = closes[-2]
                if prev_close is None or (_needs_prev_close_repair(price, prev_close) and not _same_number(history_prev_close, price)):
                    prev_close = history_prev_close
            elif prev_close is None and len(closes) == 1:
                prev_close = closes[-1]
        except Exception:
            pass

    if price is None:
        return None

    change = _pick_number(info, ["regularMarketChange"])
    if change is None and prev_close is not None:
        change = price - prev_close

    change_pct = _pick_number(info, ["regularMarketChangePercent"])
    if change_pct is None and prev_close is not None and prev_close > 0:
        change_pct = ((price - prev_close) / prev_close) * 100.0

    currency = (
        str(fast.get("currency") or "").strip().upper()
        or str(info.get("currency") or "").strip().upper()
        or (fallback_currency or "")
        or None
    )

    name = (
        str(info.get("shortName") or "").strip()
        or str(info.get("longName") or "").strip()
        or fallback_name
        or symbol
    )

    as_of = _timestamp_to_iso(_pick_number(info, ["regularMarketTime"])) or _utc_now_iso()

    snapshot: Dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "price": round(float(price), 4),
        "previousClose": round(float(prev_close), 4) if prev_close is not None else None,
        "change": round(float(change), 4) if change is not None else 0.0,
        "changePct": round(float(change_pct), 4) if change_pct is not None else 0.0,
        "currency": currency,
        "asOf": as_of,
        "source": "history" if history_price_used else "quote",
    }
    return snapshot


def _build_market_snapshot(market: str) -> Dict[str, Any]:
    config = markets_config.get(market)
    if not config:
        raise ValueError(f"Unsupported market: {market}")

    constituents: List[Dict[str, str]] = config.get("constituents", [])
    if not constituents:
        raise RuntimeError(f"Market {market} has no constituents configured")

    nodes: List[Dict[str, Any]] = []
    failures: List[str] = []

    # yfinance has internal timeout handling; keep a soft timeout to avoid very long tail.
    start = time.time()
    timed_out = False
    for idx, row in enumerate(constituents):
        symbol = row["ticker"]
        try:
            point = _fetch_one_quote(
                symbol=symbol,
                fallback_name=row.get("name") or symbol,
                fallback_sector=row.get("sector") or "Other",
                fallback_currency=config.get("currency"),
            )
            if not point:
                failures.append(symbol)
                continue
            nodes.append(point)
        except Exception as exc:
            logger.warning("Quote fetch failed for %s: %s", symbol, exc)
            failures.append(symbol)

        # Soft timeout guard to avoid long-tail hangs from upstream data vendor.
        if QUOTE_TIMEOUT_SECONDS and (time.time() - start > QUOTE_TIMEOUT_SECONDS):
            timed_out = True
            remaining = [item["ticker"] for item in constituents[idx + 1 :]]
            failures.extend(remaining)
            logger.warning(
                "Quote collection soft-timeout reached for market=%s after %ss (collected=%s skipped=%s)",
                market,
                QUOTE_TIMEOUT_SECONDS,
                len(nodes),
                len(remaining),
            )
            break

    if not nodes:
        raise RuntimeError(f"No valid quotes collected for market={market}")

    nodes.sort(key=lambda x: float(x.get("value") or 0), reverse=True)

    sector_map: Dict[str, Dict[str, Any]] = {}
    total_value = 0.0
    for node in nodes:
        sector = str(node.get("sector") or "Other")
        value = float(node.get("value") or 0)
        total_value += value
        bucket = sector_map.get(sector)
        if bucket is None:
            bucket = {"name": sector, "value": 0.0, "count": 0}
            sector_map[sector] = bucket
        bucket["value"] += value
        bucket["count"] += 1

    sectors = sorted(sector_map.values(), key=lambda x: x["value"], reverse=True)
    for item in sectors:
        item["value"] = round(float(item["value"]), 2)

    index_snapshot = None
    index_config = config.get("index")
    if isinstance(index_config, dict):
        try:
            index_snapshot = _fetch_market_index(
                symbol=str(index_config.get("ticker") or "").strip().upper(),
                fallback_name=str(index_config.get("name") or market.upper()).strip() or market.upper(),
                fallback_currency=config.get("currency"),
            )
        except Exception as exc:
            logger.warning("Index fetch failed for market=%s symbol=%s: %s", market, index_config.get("ticker"), exc)

    now_iso = _utc_now_iso()
    snapshot: Dict[str, Any] = {
        "market": market,
        "marketName": config.get("name") or market.upper(),
        "timezone": config.get("timezone") or "UTC",
        "currency": config.get("currency"),
        "description": config.get("description") or "",
        "generatedAt": now_iso,
        "source": "yfinance",
        "count": len(nodes),
        "requestedCount": len(constituents),
        "failedCount": len(failures),
        "failedTickers": failures,
        "timedOut": timed_out,
        "timeoutSeconds": QUOTE_TIMEOUT_SECONDS,
        "totalValue": round(total_value, 2),
        "sectors": sectors,
        "nodes": nodes,
    }
    if index_snapshot:
        snapshot["index"] = index_snapshot
    issue = _snapshot_completeness_issue(snapshot, market)
    if issue is not None:
        raise RuntimeError(f"Incomplete snapshot for market={market}: {issue}")
    return snapshot


def _cached_entry_valid(entry: Optional[Dict[str, Any]], now_ts: float) -> bool:
    if not entry:
        return False
    expires_at = _safe_float(entry.get("expiresAt"))
    if expires_at is None:
        return False
    return now_ts < expires_at


def _set_cache(market: str, snapshot: Dict[str, Any], source: str) -> Dict[str, Any]:
    now_ts = time.time()
    entry = {
        "snapshot": snapshot,
        "expiresAt": now_ts + CACHE_TTL_SECONDS,
        "cachedAt": now_ts,
        "source": source,
    }
    _snapshot_cache[market] = entry
    return entry


def _resolve_snapshot(market: str, force_refresh: bool = False) -> Dict[str, Any]:
    market = market.lower().strip()
    if market not in markets_config:
        raise HTTPException(status_code=404, detail=f"Unsupported market '{market}'")

    lock = _get_market_lock(market)
    acquired_lock = False
    if not force_refresh:
        acquired_lock = lock.acquire(blocking=False)
        if not acquired_lock:
            logger.info("Refresh in progress for market=%s; serving latest committed snapshot.", market)
            now_ts = time.time()
            entry = _snapshot_cache.get(market)
            cached_snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
            if _cached_entry_valid(entry, now_ts) and isinstance(cached_snapshot, dict) and _snapshot_is_complete(cached_snapshot, market):
                expires_at = _safe_float(entry.get("expiresAt")) or now_ts
                payload = dict(cached_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "memory",
                    "ttlSeconds": int(max(0, expires_at - now_ts)),
                }
                return payload

            gcs_snapshot = _load_snapshot_from_gcs(market)
            if gcs_snapshot:
                payload = dict(gcs_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "gcs",
                    "ttlSeconds": CACHE_TTL_SECONDS,
                }
                return payload

            if isinstance(cached_snapshot, dict) and _snapshot_is_complete(cached_snapshot, market):
                payload = dict(cached_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "memory-stale",
                    "ttlSeconds": 0,
                }
                payload["stale"] = True
                payload["error"] = "Refresh in progress. Serving previous committed snapshot."
                return payload

            raise HTTPException(
                status_code=503,
                detail={
                    "message": f"Snapshot not ready for market={market}.",
                    "hint": "Refresh is in progress and no committed snapshot is available yet.",
                },
            )

    if not acquired_lock:
        lock.acquire()
        acquired_lock = True
    try:
        now_ts = time.time()
        entry = _snapshot_cache.get(market)
        cached_snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
        if not force_refresh and _cached_entry_valid(entry, now_ts):
            if isinstance(cached_snapshot, dict) and _snapshot_is_complete(cached_snapshot, market):
                expires_at = _safe_float(entry.get("expiresAt")) or now_ts
                payload = dict(cached_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "memory",
                    "ttlSeconds": int(max(0, expires_at - now_ts)),
                }
                return payload
            if isinstance(cached_snapshot, dict):
                issue = _snapshot_completeness_issue(cached_snapshot, market) or "unknown issue"
                logger.warning("Bypassing incomplete memory cache for market=%s: %s", market, issue)

        if not force_refresh:
            # L2 strategy: normal read path should NOT trigger realtime recompute.
            # Frontend always reads latest snapshot prepared by scheduler/controlled refresh.
            gcs_snapshot = _load_snapshot_from_gcs(market)
            if gcs_snapshot:
                _set_cache(market, gcs_snapshot, source="gcs")
                payload = dict(gcs_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "gcs",
                    "ttlSeconds": CACHE_TTL_SECONDS,
                }
                return payload

            # No L2 snapshot yet: return stale L1 if available, otherwise explicit 503.
            if isinstance(cached_snapshot, dict) and _snapshot_is_complete(cached_snapshot, market):
                payload = dict(cached_snapshot)
                payload["cache"] = {
                    "hit": True,
                    "layer": "memory-stale",
                    "ttlSeconds": 0,
                }
                payload["stale"] = True
                payload["error"] = "No fresh snapshot in cache/GCS. Waiting for scheduler refresh."
                return payload

            raise HTTPException(
                status_code=503,
                detail={
                    "message": f"Snapshot not ready for market={market}.",
                    "hint": "Run scheduler or call refresh endpoint on GCP first.",
                },
            )

        build_error: Optional[str] = None
        try:
            snapshot = _build_market_snapshot(market)
            _set_cache(market, snapshot, source="fresh")
            try:
                _save_snapshot_to_gcs(market, snapshot)
            except Exception as exc:
                logger.exception("Failed saving snapshot to GCS (market=%s): %s", market, exc)
            payload = dict(snapshot)
            payload["cache"] = {
                "hit": False,
                "layer": "fresh",
                "ttlSeconds": CACHE_TTL_SECONDS,
            }
            return payload
        except Exception as exc:
            build_error = str(exc)
            logger.exception("Failed building fresh snapshot for market=%s: %s", market, exc)

        # Refresh path fallback 1: stale memory cache.
        if isinstance(cached_snapshot, dict) and _snapshot_is_complete(cached_snapshot, market):
            payload = dict(cached_snapshot)
            payload["cache"] = {
                "hit": True,
                "layer": "memory-stale",
                "ttlSeconds": 0,
            }
            payload["stale"] = True
            payload["error"] = build_error
            return payload

        # Refresh path fallback 2: latest GCS snapshot.
        gcs_snapshot = _load_snapshot_from_gcs(market)
        if gcs_snapshot:
            _set_cache(market, gcs_snapshot, source="gcs")
            payload = dict(gcs_snapshot)
            payload["cache"] = {
                "hit": True,
                "layer": "gcs",
                "ttlSeconds": CACHE_TTL_SECONDS,
            }
            payload["stale"] = True
            payload["error"] = build_error
            return payload

        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Failed resolving heatmap snapshot for market={market}",
                "error": build_error,
            },
        )
    finally:
        if acquired_lock:
            lock.release()


def _require_refresh_token(request: Request) -> None:
    if not CRON_TOKEN:
        return
    provided = (request.headers.get("x-heatmap-token") or "").strip()
    if not provided:
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if provided != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@app.on_event("startup")
def _startup() -> None:
    global markets_config
    markets_config = _load_markets_config()
    logger.info(
        "Heatmap service started. markets=%s cache_ttl=%ss gcs_bucket=%s",
        ",".join(sorted(markets_config.keys())),
        CACHE_TTL_SECONDS,
        GCS_BUCKET_NAME or "<disabled>",
    )


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "generatedAt": _utc_now_iso(),
        "markets": sorted(markets_config.keys()),
        "defaultMarket": DEFAULT_MARKET,
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "gcsEnabled": bool(GCS_BUCKET_NAME),
    }


@app.get("/v1/markets")
def list_markets() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for key in sorted(markets_config.keys()):
        cfg = markets_config[key]
        items.append(
            {
                "market": key,
                "name": cfg.get("name") or key.upper(),
                "timezone": cfg.get("timezone") or "UTC",
                "currency": cfg.get("currency"),
                "constituentCount": len(cfg.get("constituents") or []),
                "description": cfg.get("description") or "",
            }
        )
    return {"markets": items, "defaultMarket": DEFAULT_MARKET}


@app.get("/v1/heatmap/{market}")
def get_heatmap_snapshot(market: str) -> JSONResponse:
    payload = _resolve_snapshot(market=market, force_refresh=False)
    headers = {
        "cache-control": "public, max-age=10, s-maxage=30, stale-while-revalidate=120",
        "x-heatmap-market": market.lower(),
    }
    return JSONResponse(payload, headers=headers)


@app.post("/v1/heatmap/{market}/refresh")
def refresh_market_snapshot(request: Request, market: str) -> Dict[str, Any]:
    _require_refresh_token(request)
    payload = _resolve_snapshot(market=market, force_refresh=True)
    cache_info = payload.get("cache") if isinstance(payload, dict) else {}
    cache_layer = str(cache_info.get("layer") or "").strip().lower() if isinstance(cache_info, dict) else ""
    if cache_layer != "fresh" or _coerce_bool(payload.get("stale")):
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Failed refreshing heatmap snapshot for market={market}",
                "error": payload.get("error") or "Refresh fell back to stale snapshot",
            },
        )
    return payload


@app.post("/v1/heatmap/refresh_all")
async def refresh_all_snapshots(request: Request) -> Dict[str, Any]:
    _require_refresh_token(request)

    body: Dict[str, Any] = {}
    if request.headers.get("content-length"):
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
    requested_markets = body.get("markets") if isinstance(body, dict) else None
    force_refresh = _coerce_bool(body.get("force")) if isinstance(body, dict) else False

    if isinstance(requested_markets, list) and requested_markets:
        markets = [str(m).strip().lower() for m in requested_markets if str(m).strip()]
    else:
        markets = sorted(markets_config.keys())

    now_utc = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    for market in markets:
        decision = _refresh_decision(market, force=force_refresh, now=now_utc)
        if not decision.get("refresh"):
            results.append(
                {
                    "market": market,
                    "ok": True,
                    "refreshed": False,
                    "reason": decision.get("reason"),
                    "cadenceSeconds": decision.get("cadenceSeconds"),
                    "phaseMinute": decision.get("phaseMinute"),
                    "ageSeconds": decision.get("ageSeconds"),
                }
            )
            continue

        try:
            snap = _resolve_snapshot(market=market, force_refresh=True)
            cache_info = snap.get("cache") if isinstance(snap, dict) else {}
            cache_layer = str(cache_info.get("layer") or "").strip().lower() if isinstance(cache_info, dict) else ""
            if cache_layer != "fresh" or _coerce_bool(snap.get("stale")):
                results.append(
                    {
                        "market": market,
                        "ok": False,
                        "refreshed": False,
                        "status": 502,
                        "detail": {
                            "message": f"Refresh did not produce a complete snapshot for market={market}",
                            "error": snap.get("error") or "Refresh fell back to stale snapshot",
                        },
                    }
                )
                continue
            results.append(
                {
                    "market": market,
                    "ok": True,
                    "refreshed": True,
                    "reason": decision.get("reason"),
                    "count": snap.get("count"),
                    "failedCount": snap.get("failedCount"),
                    "generatedAt": snap.get("generatedAt"),
                    "cadenceSeconds": decision.get("cadenceSeconds"),
                    "phaseMinute": decision.get("phaseMinute"),
                    "ageSeconds": decision.get("ageSeconds"),
                }
            )
        except HTTPException as exc:
            results.append({"market": market, "ok": False, "status": exc.status_code, "detail": exc.detail})
        except Exception as exc:
            results.append({"market": market, "ok": False, "status": 500, "detail": str(exc)})

    ok_count = sum(1 for item in results if item.get("ok"))
    refreshed_count = sum(1 for item in results if item.get("refreshed"))
    skipped_count = sum(1 for item in results if item.get("ok") and not item.get("refreshed"))
    return {
        "ok": ok_count == len(results),
        "force": force_refresh,
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "refreshed": refreshed_count,
        "skipped": skipped_count,
        "results": results,
    }
