from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .service import ReportSourceService

logger = logging.getLogger(__name__)

router = APIRouter()

_MONITOR: Optional["ReportSourceMonitor"] = None
_ROUTER_REGISTERED = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    target = dt or _utc_now()
    return target.isoformat().replace("+00:00", "Z")


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return int(default)
    try:
        val = int(raw)
    except Exception:
        return int(default)
    return max(minimum, min(maximum, val))


def _normalize_tickers(raw: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in raw or []:
        t = str(item or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _parse_tickers_env(name: str) -> List[str]:
    raw = str(os.environ.get(name, "") or "")
    if not raw.strip():
        return []
    parts = []
    for seg in raw.replace("\n", ",").split(","):
        s = seg.strip()
        if s:
            parts.append(s)
    return _normalize_tickers(parts)


class ReportSourceMonitorConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    earnings_day_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    normal_mode: Optional[str] = Field(default=None, description="off|weekly|interval")
    normal_interval_minutes: Optional[int] = Field(default=None, ge=60, le=43200)
    tickers: Optional[List[str]] = None
    run_on_startup: Optional[bool] = None


class ReportSourceMonitor:
    _ALLOWED_NORMAL_MODES = {"off", "weekly", "interval"}

    def __init__(
        self,
        *,
        get_report_source_service: Callable[[], ReportSourceService],
        get_next_earnings_date: Callable[[str], Optional[date]],
        get_today_date: Callable[[], date],
        default_tickers: List[str],
        state_file_path: str,
    ) -> None:
        self._get_report_source_service = get_report_source_service
        self._get_next_earnings_date = get_next_earnings_date
        self._get_today_date = get_today_date
        self._state_path = Path(state_file_path).resolve()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

        env_tickers = _parse_tickers_env("REPORT_SOURCE_MONITOR_TICKERS")
        initial_tickers = env_tickers if env_tickers else _normalize_tickers(default_tickers)
        normal_mode_raw = str(os.environ.get("REPORT_SOURCE_MONITOR_NORMAL_MODE", "off")).strip().lower() or "off"
        normal_mode = normal_mode_raw if normal_mode_raw in self._ALLOWED_NORMAL_MODES else "off"

        self._config: Dict[str, Any] = {
            "enabled": _parse_bool_env("REPORT_SOURCE_MONITOR_ENABLED", False),
            "earnings_day_interval_minutes": _parse_int_env(
                "REPORT_SOURCE_MONITOR_EARNINGS_DAY_INTERVAL_MINUTES",
                60,
                5,
                1440,
            ),
            "normal_mode": normal_mode,
            "normal_interval_minutes": _parse_int_env(
                "REPORT_SOURCE_MONITOR_NORMAL_INTERVAL_MINUTES",
                7 * 24 * 60,
                60,
                43200,
            ),
            "run_on_startup": _parse_bool_env("REPORT_SOURCE_MONITOR_RUN_ON_STARTUP", True),
            "tickers": initial_tickers,
            "updated_at": _utc_iso(),
        }

        self._config_lock = Lock()
        self._state_lock = Lock()
        self._run_lock = Lock()
        self._wake_event = Event()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None

        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []
        self._runtime: Dict[str, Any] = {
            "worker_running": False,
            "last_run_at": None,
            "last_reason": None,
            "last_result": None,
            "last_error": "",
            "phase": "idle",
            "next_run_at": None,
            "earnings_day_tickers": [],
        }
        self._load_state()

    def _state_payload(self) -> Dict[str, Any]:
        with self._config_lock:
            cfg = dict(self._config)
        with self._state_lock:
            runtime = dict(self._runtime)
            snapshots = dict(self._snapshots)
            events = list(self._events[-200:])
        return {
            "config": cfg,
            "runtime": runtime,
            "snapshots": snapshots,
            "events": events,
            "saved_at": _utc_iso(),
        }

    def _persist_state(self) -> None:
        payload = self._state_payload()
        tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self._state_path)
        except Exception as exc:
            logger.warning("report_source monitor failed to persist state: %s", exc)

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        cfg = payload.get("config")
        if isinstance(cfg, dict):
            patch = {}
            if isinstance(cfg.get("enabled"), bool):
                patch["enabled"] = cfg.get("enabled")
            if isinstance(cfg.get("earnings_day_interval_minutes"), int):
                patch["earnings_day_interval_minutes"] = max(5, min(1440, int(cfg.get("earnings_day_interval_minutes"))))
            mode = str(cfg.get("normal_mode") or "").strip().lower()
            if mode in self._ALLOWED_NORMAL_MODES:
                patch["normal_mode"] = mode
            if isinstance(cfg.get("normal_interval_minutes"), int):
                patch["normal_interval_minutes"] = max(60, min(43200, int(cfg.get("normal_interval_minutes"))))
            if isinstance(cfg.get("run_on_startup"), bool):
                patch["run_on_startup"] = cfg.get("run_on_startup")
            if isinstance(cfg.get("tickers"), list):
                patch["tickers"] = _normalize_tickers([str(x) for x in cfg.get("tickers")])
            if patch:
                with self._config_lock:
                    self._config.update(patch)
                    self._config["updated_at"] = _utc_iso()

        snapshots = payload.get("snapshots")
        if isinstance(snapshots, dict):
            normalized: Dict[str, Dict[str, Any]] = {}
            for k, v in snapshots.items():
                if not isinstance(v, dict):
                    continue
                normalized[str(k)] = dict(v)
            with self._state_lock:
                self._snapshots = normalized

        events = payload.get("events")
        if isinstance(events, list):
            clean_events = [dict(e) for e in events if isinstance(e, dict)]
            with self._state_lock:
                self._events = clean_events[-200:]

    def _set_runtime(self, **kwargs: Any) -> None:
        with self._state_lock:
            self._runtime.update(kwargs)

    def _get_config(self) -> Dict[str, Any]:
        with self._config_lock:
            return dict(self._config)

    def _build_status(self) -> Dict[str, Any]:
        with self._config_lock:
            cfg = dict(self._config)
        with self._state_lock:
            runtime = dict(self._runtime)
            recent_events = list(self._events[-20:])
            snapshots_count = len(self._snapshots)
        runtime["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return {
            "config": cfg,
            "runtime": runtime,
            "snapshots_count": snapshots_count,
            "recent_events": recent_events,
            "state_file": str(self._state_path),
        }

    def start(self) -> Dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return {"started": False, "message": "already_running", "status": self._build_status()}
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = Thread(target=self._worker_loop, daemon=True, name="report-source-monitor")
        self._thread.start()
        self._set_runtime(worker_running=True, phase="starting")
        return {"started": True, "message": "started", "status": self._build_status()}

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        self._wake_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)
        self._set_runtime(worker_running=False, phase="stopped", next_run_at=None)
        self._persist_state()
        return {"stopped": True, "status": self._build_status()}

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        updates: Dict[str, Any] = {}
        if "enabled" in patch and isinstance(patch.get("enabled"), bool):
            updates["enabled"] = patch.get("enabled")
        if "earnings_day_interval_minutes" in patch and patch.get("earnings_day_interval_minutes") is not None:
            updates["earnings_day_interval_minutes"] = max(5, min(1440, int(patch.get("earnings_day_interval_minutes"))))
        if "normal_mode" in patch and patch.get("normal_mode") is not None:
            mode = str(patch.get("normal_mode") or "").strip().lower()
            if mode not in self._ALLOWED_NORMAL_MODES:
                raise ValueError("normal_mode must be one of: off, weekly, interval")
            updates["normal_mode"] = mode
        if "normal_interval_minutes" in patch and patch.get("normal_interval_minutes") is not None:
            updates["normal_interval_minutes"] = max(60, min(43200, int(patch.get("normal_interval_minutes"))))
        if "tickers" in patch and patch.get("tickers") is not None:
            tickers = patch.get("tickers")
            if not isinstance(tickers, list):
                raise ValueError("tickers must be a list")
            updates["tickers"] = _normalize_tickers([str(x) for x in tickers])
        if "run_on_startup" in patch and isinstance(patch.get("run_on_startup"), bool):
            updates["run_on_startup"] = patch.get("run_on_startup")

        if updates:
            with self._config_lock:
                self._config.update(updates)
                self._config["updated_at"] = _utc_iso()
        self._persist_state()
        self._wake_event.set()
        return self._build_status()

    def _detect_phase(self, tickers: List[str]) -> Tuple[str, List[str]]:
        earnings_day_tickers: List[str] = []
        try:
            today_local = self._get_today_date()
        except Exception:
            today_local = _utc_now().date()
        for ticker in tickers:
            try:
                d = self._get_next_earnings_date(ticker)
            except Exception:
                continue
            if isinstance(d, date) and d == today_local:
                earnings_day_tickers.append(ticker)
        if earnings_day_tickers:
            return "earnings_day", earnings_day_tickers
        return "normal", []

    def _next_interval_seconds(self, cfg: Dict[str, Any], phase: str) -> Optional[int]:
        if phase == "earnings_day":
            minutes = int(cfg.get("earnings_day_interval_minutes") or 60)
            return max(5, minutes) * 60
        mode = str(cfg.get("normal_mode") or "off").strip().lower()
        if mode == "off":
            return None
        if mode == "weekly":
            return 7 * 24 * 60 * 60
        minutes = int(cfg.get("normal_interval_minutes") or (7 * 24 * 60))
        return max(60, minutes) * 60

    @staticmethod
    def _snapshot_fingerprint(status_code: Optional[int], final_url: str, title: str, text: str) -> str:
        payload = f"{status_code}|{final_url}|{title}|{text[:12000]}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _run_once_internal(self, reason: str) -> Dict[str, Any]:
        cfg = self._get_config()
        tickers = _normalize_tickers([str(t) for t in cfg.get("tickers") or []])
        if not tickers:
            result = {
                "ok": True,
                "reason": reason,
                "tickers": 0,
                "checked_urls": 0,
                "changes": 0,
                "message": "no_tickers_configured",
                "at": _utc_iso(),
            }
            self._set_runtime(last_run_at=result["at"], last_reason=reason, last_result=result, last_error="")
            self._persist_state()
            return result

        service = self._get_report_source_service()
        changes: List[Dict[str, Any]] = []
        checked_urls = 0
        newly_seeded = 0

        for ticker in tickers:
            source_payload = service.storage.load(ticker, 0)
            if not isinstance(source_payload, dict):
                try:
                    source_payload = service.resolve(ticker, force_refresh=False)
                except Exception:
                    source_payload = None
            if not isinstance(source_payload, dict):
                continue

            urls = [
                ("ir_home", str(source_payload.get("ir_home_url") or "").strip()),
                ("financial_reports", str(source_payload.get("financial_reports_url") or "").strip()),
                ("sec_filings", str(source_payload.get("sec_filings_url") or "").strip()),
            ]
            for kind, url in urls:
                if not url:
                    continue
                checked_urls += 1
                snap = service.fetcher.fetch_page(url)
                status_code = snap.status_code if snap else None
                final_url = str(snap.final_url if snap else url)
                title = str(snap.title if snap else "")
                text = str(snap.text if snap else "")
                fingerprint = self._snapshot_fingerprint(status_code, final_url, title, text)

                key = f"{ticker}|{kind}|{url}"
                prev = self._snapshots.get(key) if isinstance(self._snapshots.get(key), dict) else None
                prev_fp = str(prev.get("fingerprint") or "") if prev else ""
                if not prev:
                    newly_seeded += 1

                self._snapshots[key] = {
                    "ticker": ticker,
                    "kind": kind,
                    "url": url,
                    "final_url": final_url,
                    "status_code": status_code,
                    "title": title[:240],
                    "fingerprint": fingerprint,
                    "checked_at": _utc_iso(),
                }

                if prev and prev_fp and prev_fp != fingerprint:
                    changes.append(
                        {
                            "ticker": ticker,
                            "kind": kind,
                            "url": url,
                            "final_url": final_url,
                            "status_code": status_code,
                            "checked_at": _utc_iso(),
                            "change_type": "content_or_status_changed",
                            "previous_checked_at": prev.get("checked_at"),
                        }
                    )

        now_iso = _utc_iso()
        if changes:
            with self._state_lock:
                self._events.extend(changes)
                self._events = self._events[-200:]
        result = {
            "ok": True,
            "reason": reason,
            "tickers": len(tickers),
            "checked_urls": checked_urls,
            "changes": len(changes),
            "seeded_snapshots": newly_seeded,
            "at": now_iso,
        }
        self._set_runtime(last_run_at=now_iso, last_reason=reason, last_result=result, last_error="")
        self._persist_state()
        return result

    def run_once(self, reason: str = "manual") -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "ok": False,
                "reason": reason,
                "message": "already_running",
                "at": _utc_iso(),
            }
        try:
            return self._run_once_internal(reason=reason)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._set_runtime(last_error=err)
            self._persist_state()
            return {
                "ok": False,
                "reason": reason,
                "message": err,
                "at": _utc_iso(),
            }
        finally:
            self._run_lock.release()

    def _worker_loop(self) -> None:
        next_run_at: Optional[datetime] = None
        try:
            self._set_runtime(worker_running=True, phase="idle")
            cfg = self._get_config()
            if bool(cfg.get("run_on_startup")):
                next_run_at = _utc_now()
            while not self._stop_event.is_set():
                cfg = self._get_config()
                if not bool(cfg.get("enabled")):
                    self._set_runtime(phase="disabled", next_run_at=None, earnings_day_tickers=[])
                    self._wake_event.wait(timeout=60.0)
                    self._wake_event.clear()
                    continue

                now = _utc_now()
                if next_run_at is None or now >= next_run_at:
                    phase, earnings_day_tickers = self._detect_phase(_normalize_tickers(cfg.get("tickers") or []))
                    self._set_runtime(phase=phase, earnings_day_tickers=earnings_day_tickers)
                    _ = self.run_once(reason="scheduled")
                    interval_sec = self._next_interval_seconds(cfg, phase)
                    if interval_sec is None:
                        next_run_at = None
                        self._set_runtime(next_run_at=None)
                        self._wake_event.wait(timeout=300.0)
                        self._wake_event.clear()
                        continue
                    next_run_at = _utc_now() + timedelta(seconds=interval_sec)
                    self._set_runtime(next_run_at=_utc_iso(next_run_at))

                wait_seconds = 1.0
                if next_run_at is not None:
                    wait_seconds = max(1.0, min((next_run_at - _utc_now()).total_seconds(), 3600.0))
                self._wake_event.wait(timeout=wait_seconds)
                self._wake_event.clear()
        finally:
            self._set_runtime(worker_running=False, phase="stopped", next_run_at=None, earnings_day_tickers=[])
            self._persist_state()

    def status(self) -> Dict[str, Any]:
        return self._build_status()


def _get_monitor() -> ReportSourceMonitor:
    if _MONITOR is None:
        raise HTTPException(status_code=500, detail="report_source monitor is not configured")
    return _MONITOR


@router.get("/stockflow/report_source/monitor/status", summary="查看官方财报 URL 监听状态")
async def report_source_monitor_status() -> Dict[str, Any]:
    return _get_monitor().status()


@router.post("/stockflow/report_source/monitor/config", summary="更新官方财报 URL 监听配置")
async def report_source_monitor_config(payload: ReportSourceMonitorConfigPatch) -> Dict[str, Any]:
    monitor = _get_monitor()
    try:
        return await asyncio.to_thread(monitor.update_config, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stockflow/report_source/monitor/run_once", summary="手动执行一次官方财报 URL 监听")
async def report_source_monitor_run_once() -> Dict[str, Any]:
    monitor = _get_monitor()
    return await asyncio.to_thread(monitor.run_once, "manual_api")


def register_report_source_monitor_routes(
    app: Any,
    *,
    default_tickers: List[str],
    get_report_source_service: Callable[[], ReportSourceService],
    get_next_earnings_date: Callable[[str], Optional[date]],
    get_today_date: Callable[[], date],
    state_file_path: str,
) -> None:
    global _MONITOR, _ROUTER_REGISTERED
    _MONITOR = ReportSourceMonitor(
        get_report_source_service=get_report_source_service,
        get_next_earnings_date=get_next_earnings_date,
        get_today_date=get_today_date,
        default_tickers=default_tickers,
        state_file_path=state_file_path,
    )
    if _ROUTER_REGISTERED:
        return
    app.include_router(router)
    _ROUTER_REGISTERED = True


def start_report_source_monitor() -> Dict[str, Any]:
    monitor = _get_monitor()
    return monitor.start()


def stop_report_source_monitor() -> Dict[str, Any]:
    monitor = _get_monitor()
    return monitor.stop()
