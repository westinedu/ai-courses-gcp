#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from report_source import ReportSourceService


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_tickers(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Ticker file must be a JSON list: {path}")

    out: List[str] = []
    seen = set()
    for raw in data:
        t = str(raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _parse_args() -> argparse.Namespace:
    default_ticker_file = (
        Path(__file__).resolve().parents[3] / "AWS" / "ticker-manager" / "us_tickers.json"
    )
    parser = argparse.ArgumentParser(
        description="Resolve report source URLs for tickers from a JSON list."
    )
    parser.add_argument(
        "--ticker-file",
        default=str(default_ticker_file),
        help="Path to JSON ticker list (default: AWS/ticker-manager/us_tickers.json).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force refresh even when cache is still valid.",
    )
    parser.add_argument(
        "--max-count",
        type=int,
        default=0,
        help="Only process first N tickers (0 means all).",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start from this index in ticker list (0-based).",
    )
    parser.add_argument(
        "--log-path",
        default="data/report_source_batch_progress.jsonl",
        help="Progress log path (JSONL).",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GCS_BUCKET_NAME", "").strip(),
        help="Override GCS bucket name. Defaults to env GCS_BUCKET_NAME.",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("REPORT_SOURCE_PREFIX", "report_sources").strip(),
        help="Override report source prefix. Defaults to env REPORT_SOURCE_PREFIX or report_sources.",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=int(os.environ.get("REPORT_SOURCE_CACHE_TTL_SECONDS", "86400")),
        help="Cache TTL seconds for resolve operation.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=int(os.environ.get("REPORT_SOURCE_MAX_CANDIDATES", "24")),
        help="Max candidates per ticker.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    ticker_file = Path(args.ticker_file).resolve()
    if not ticker_file.exists():
        raise SystemExit(f"Ticker file not found: {ticker_file}")

    tickers = _load_tickers(ticker_file)
    if args.start_index > 0:
        if args.start_index >= len(tickers):
            raise SystemExit(
                f"--start-index {args.start_index} out of range (ticker count {len(tickers)})"
            )
        tickers = tickers[args.start_index :]
    if args.max_count > 0:
        tickers = tickers[: args.max_count]
    if not tickers:
        print("[resolve] no tickers to process")
        return 0

    root_dir = Path(__file__).resolve().parents[1]
    data_dir = root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_path)
    if not log_path.is_absolute():
        log_path = (root_dir / log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    service = ReportSourceService(
        bucket_name=args.bucket,
        local_data_dir=str(data_dir),
        prefix=args.prefix,
        cache_ttl_seconds=max(0, int(args.cache_ttl_seconds)),
        max_candidates=max(8, int(args.max_candidates)),
    )

    total = len(tickers)
    ok = 0
    failed = 0
    print(
        f"[resolve] start total={total} force_refresh={bool(args.force_refresh)} "
        f"bucket={'(none)' if not args.bucket else args.bucket} prefix={args.prefix}"
    )
    print(f"[resolve] ticker_file={ticker_file}")
    print(f"[resolve] log_path={log_path}")

    with log_path.open("a", encoding="utf-8") as logf:
        for i, ticker in enumerate(tickers, start=1):
            try:
                payload = service.resolve(ticker, force_refresh=bool(args.force_refresh))
                status = str(payload.get("verification_status") or "")
                conf = payload.get("confidence")
                ir = payload.get("ir_home_url")
                reports = payload.get("financial_reports_url")
                sec = payload.get("sec_filings_url")
                print(
                    f"[{i}/{total}] {ticker} status={status} conf={conf} "
                    f"ir={'yes' if ir else 'no'} reports={'yes' if reports else 'no'} sec={'yes' if sec else 'no'}"
                )
                ok += 1
                row: Dict[str, Any] = {
                    "at": _now_iso(),
                    "ticker": ticker,
                    "ok": True,
                    "status": status,
                    "confidence": conf,
                    "ir_home_url": ir,
                    "financial_reports_url": reports,
                    "sec_filings_url": sec,
                }
            except Exception as exc:
                failed += 1
                err = f"{type(exc).__name__}: {exc}"
                print(f"[{i}/{total}] {ticker} ERROR {err}", file=sys.stderr)
                row = {
                    "at": _now_iso(),
                    "ticker": ticker,
                    "ok": False,
                    "error": err,
                }

            logf.write(json.dumps(row, ensure_ascii=False) + "\n")
            logf.flush()

    try:
        service.fetcher.close()
    except Exception:
        pass

    print(f"[resolve] done total={total} ok={ok} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
