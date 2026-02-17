#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google.cloud import storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync local report_source cache files to GCS for StockFlow."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing *_report_source.json files (default: data).",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GCS_BUCKET_NAME", "").strip(),
        help="Target GCS bucket. Defaults to env GCS_BUCKET_NAME.",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("REPORT_SOURCE_PREFIX", "report_sources").strip(),
        help="Target prefix in bucket (default: report_sources or env REPORT_SOURCE_PREFIX).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned uploads without writing to GCS.",
    )
    return parser.parse_args()


def iter_local_files(data_dir: Path):
    if not data_dir.exists():
        return
    for path in sorted(data_dir.glob("*_report_source.json")):
        if not path.is_file():
            continue
        ticker = path.name.replace("_report_source.json", "").upper()
        if not ticker:
            continue
        yield ticker, path


def main() -> int:
    args = parse_args()
    if not args.bucket:
        raise SystemExit("Missing --bucket (or set GCS_BUCKET_NAME).")

    data_dir = Path(args.data_dir).resolve()
    prefix = str(args.prefix or "report_sources").strip().strip("/")
    storage_client = storage.Client()
    bucket = storage_client.bucket(args.bucket)

    count = 0
    for ticker, path in iter_local_files(data_dir):
        blob_name = f"{prefix}/{ticker}.json"
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            print(f"[skip] {path} (not a json object)")
            continue

        if args.dry_run:
            print(f"[dry-run] {path} -> gs://{args.bucket}/{blob_name}")
        else:
            blob = bucket.blob(blob_name)
            blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
            print(f"[uploaded] {path} -> gs://{args.bucket}/{blob_name}")
        count += 1

    print(f"[done] processed={count} data_dir={data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

