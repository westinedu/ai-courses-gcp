from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud import storage


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class ReportSourceStorage:
    def __init__(self, bucket_name: str, local_data_dir: str, prefix: str = "report_sources") -> None:
        self.bucket_name = (bucket_name or "").strip()
        self.local_data_dir = local_data_dir
        self.prefix = (prefix or "report_sources").strip().strip("/")
        os.makedirs(local_data_dir, exist_ok=True)

    def _blob_name(self, ticker: str) -> str:
        return f"{self.prefix}/{ticker.upper()}.json"

    def _local_path(self, ticker: str) -> str:
        filename = f"{ticker.upper()}_report_source.json"
        return os.path.join(self.local_data_dir, filename)

    def load(self, ticker: str, max_age_seconds: int = 0) -> Optional[Dict[str, Any]]:
        ticker = ticker.upper()

        obj = self._load_from_gcs(ticker)
        if obj is None:
            obj = self._load_from_local(ticker)
        if obj is None:
            return None

        if max_age_seconds > 0:
            discovered_at = _parse_iso(obj.get("discovered_at"))
            if discovered_at is None:
                return None
            age = (datetime.now(timezone.utc) - discovered_at).total_seconds()
            if age > max_age_seconds:
                return None
        return obj

    def save(self, ticker: str, payload: Dict[str, Any]) -> Dict[str, str]:
        ticker = ticker.upper()
        out: Dict[str, str] = {}

        gcs_path = self._save_to_gcs(ticker, payload)
        if gcs_path:
            out["gcs_path"] = gcs_path

        local_path = self._save_to_local(ticker, payload)
        if local_path:
            out["local_path"] = local_path

        return out

    def _load_from_gcs(self, ticker: str) -> Optional[Dict[str, Any]]:
        if not self.bucket_name:
            return None
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(self.bucket_name)
            blob = bucket.blob(self._blob_name(ticker))
            if not blob.exists():
                return None
            raw = blob.download_as_text(encoding="utf-8")
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def list_records(self, limit: int = 500, ticker_prefix: str = "") -> List[Dict[str, Any]]:
        prefix_norm = (ticker_prefix or "").strip().upper()
        records = self._list_from_gcs(limit=limit, ticker_prefix=prefix_norm)
        if records:
            return records
        return self._list_from_local(limit=limit, ticker_prefix=prefix_norm)

    def _list_from_gcs(self, limit: int, ticker_prefix: str) -> List[Dict[str, Any]]:
        if not self.bucket_name:
            return []
        out: List[Dict[str, Any]] = []
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(self.bucket_name)
            blob_prefix = f"{self.prefix}/"
            # Fetch more than limit to account for filters / malformed records.
            max_results = max(limit * 3, limit + 50)
            blobs = bucket.list_blobs(prefix=blob_prefix, max_results=max_results)
            for blob in blobs:
                name = str(blob.name or "")
                if not name.endswith(".json"):
                    continue
                ticker = os.path.basename(name)[:-5].upper()
                if ticker_prefix and not ticker.startswith(ticker_prefix):
                    continue
                try:
                    raw = blob.download_as_text(encoding="utf-8")
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                obj.setdefault("ticker", ticker)
                out.append(obj)
                if len(out) >= limit:
                    break
        except Exception:
            return []
        return sorted(out, key=lambda x: str(x.get("ticker", "")))

    def _save_to_gcs(self, ticker: str, payload: Dict[str, Any]) -> str:
        if not self.bucket_name:
            return ""
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(self.bucket_name)
            blob_name = self._blob_name(ticker)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
            return f"gs://{self.bucket_name}/{blob_name}"
        except Exception:
            return ""

    def _load_from_local(self, ticker: str) -> Optional[Dict[str, Any]]:
        path = self._local_path(ticker)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _list_from_local(self, limit: int, ticker_prefix: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            for name in sorted(os.listdir(self.local_data_dir)):
                if not name.endswith("_report_source.json"):
                    continue
                ticker = name.replace("_report_source.json", "").upper()
                if ticker_prefix and not ticker.startswith(ticker_prefix):
                    continue
                path = os.path.join(self.local_data_dir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        obj = json.load(f)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                obj.setdefault("ticker", ticker)
                out.append(obj)
                if len(out) >= limit:
                    break
        except Exception:
            return []
        return out

    def _save_to_local(self, ticker: str, payload: Dict[str, Any]) -> str:
        path = self._local_path(ticker)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return path
        except Exception:
            return ""
