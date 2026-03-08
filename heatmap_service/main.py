from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
QUOTE_TIMEOUT_SECONDS = max(4, int(os.environ.get("HEATMAP_QUOTE_TIMEOUT_SECONDS", "18")))

LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "markets.json"

_storage_client: Optional[storage.Client] = None
_config_lock = threading.Lock()
_snapshot_cache: Dict[str, Dict[str, Any]] = {}
_snapshot_locks: Dict[str, threading.Lock] = {}
markets_config: Dict[str, Any] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    payload = remote_payload or local_payload

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
            "constituents": clean_constituents,
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


def _load_snapshot_from_gcs(market: str) -> Optional[Dict[str, Any]]:
    client = _storage()
    if client is None:
        return None
    path = _latest_blob_path(market)
    try:
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(path)
        if not blob.exists(client=client):
            return None
        data = json.loads(blob.download_as_text())
        if not isinstance(data, dict):
            return None
        data["_from"] = "gcs"
        return data
    except Exception as exc:
        logger.exception("Failed loading GCS snapshot (%s): %s", path, exc)
        return None


def _save_snapshot_to_gcs(market: str, snapshot: Dict[str, Any]) -> None:
    client = _storage()
    if client is None:
        return
    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    bucket = client.bucket(GCS_BUCKET_NAME)

    latest_path = _latest_blob_path(market)
    bucket.blob(latest_path).upload_from_string(body, content_type="application/json")

    if WRITE_HISTORY:
        generated_at = str(snapshot.get("generatedAt") or _utc_now_iso())
        history_path = _history_blob_path(market, generated_at)
        bucket.blob(history_path).upload_from_string(body, content_type="application/json")


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
    if price is None or prev_close is None or market_cap is None:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}

    if price is None:
        price = _pick_number(info, ["regularMarketPrice", "currentPrice"])
    if prev_close is None:
        prev_close = _pick_number(info, ["regularMarketPreviousClose", "previousClose"])
    if market_cap is None:
        market_cap = _pick_number(info, ["marketCap"])

    history_price_used = False
    if price is None or prev_close is None:
        try:
            hist = ticker_obj.history(period="2d", interval="1d", auto_adjust=False, actions=False)
            closes = []
            if hist is not None and not hist.empty and "Close" in hist.columns:
                closes = [
                    _safe_float(x) for x in hist["Close"].tolist() if _safe_float(x) is not None
                ]
            if price is None and closes:
                price = closes[-1]
                history_price_used = True
            if prev_close is None:
                if len(closes) >= 2:
                    prev_close = closes[-2]
                elif len(closes) == 1:
                    prev_close = closes[-1]
        except Exception:
            pass

    if price is None:
        return None

    change_pct = None
    if prev_close is not None and prev_close > 0:
        change_pct = ((price - prev_close) / prev_close) * 100.0
    if change_pct is None:
        change_pct = _pick_number(info, ["regularMarketChangePercent"]) or 0.0

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


def _build_market_snapshot(market: str) -> Dict[str, Any]:
    config = markets_config.get(market)
    if not config:
        raise ValueError(f"Unsupported market: {market}")

    constituents: List[Dict[str, str]] = config.get("constituents", [])
    if not constituents:
        raise RuntimeError(f"Market {market} has no constituents configured")

    nodes: List[Dict[str, Any]] = []
    failures: List[str] = []

    # yfinance has internal timeout handling; network spikes should not block too long.
    start = time.time()
    for row in constituents:
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
        if time.time() - start > QUOTE_TIMEOUT_SECONDS:
            logger.warning("Quote collection soft-timeout reached for market=%s after %ss", market, QUOTE_TIMEOUT_SECONDS)
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
        "totalValue": round(total_value, 2),
        "sectors": sectors,
        "nodes": nodes,
    }
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
    with lock:
        now_ts = time.time()
        entry = _snapshot_cache.get(market)
        if not force_refresh and _cached_entry_valid(entry, now_ts):
            expires_at = _safe_float(entry.get("expiresAt")) or now_ts
            payload = dict(entry["snapshot"])
            payload["cache"] = {
                "hit": True,
                "layer": "memory",
                "ttlSeconds": int(max(0, expires_at - now_ts)),
            }
            return payload

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
            if entry and isinstance(entry.get("snapshot"), dict):
                payload = dict(entry["snapshot"])
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
        if entry and isinstance(entry.get("snapshot"), dict):
            payload = dict(entry["snapshot"])
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
    return _resolve_snapshot(market=market, force_refresh=True)


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

    if isinstance(requested_markets, list) and requested_markets:
        markets = [str(m).strip().lower() for m in requested_markets if str(m).strip()]
    else:
        markets = sorted(markets_config.keys())

    results: List[Dict[str, Any]] = []
    for market in markets:
        try:
            snap = _resolve_snapshot(market=market, force_refresh=True)
            results.append(
                {
                    "market": market,
                    "ok": True,
                    "count": snap.get("count"),
                    "failedCount": snap.get("failedCount"),
                    "generatedAt": snap.get("generatedAt"),
                }
            )
        except HTTPException as exc:
            results.append({"market": market, "ok": False, "status": exc.status_code, "detail": exc.detail})
        except Exception as exc:
            results.append({"market": market, "ok": False, "status": 500, "detail": str(exc)})

    ok_count = sum(1 for item in results if item.get("ok"))
    return {
        "ok": ok_count == len(results),
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
