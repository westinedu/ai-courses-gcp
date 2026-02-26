from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request
except Exception:  # pragma: no cover
    google_auth_default = None
    Request = None


class VertexAIStructuredExtractor:
    """Optional Vertex AI extractor for deep earnings-document analysis."""

    def __init__(self) -> None:
        self.enabled = str(os.environ.get("REPORT_SOURCE_ENABLE_AI", "0")).strip().lower() in {"1", "true", "yes"}
        self.project = (
            os.environ.get("VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or ""
        ).strip()
        self.location = (os.environ.get("VERTEX_LOCATION") or "us-central1").strip()
        self.model = (
            os.environ.get("REPORT_SOURCE_EXTRACTION_MODEL")
            or os.environ.get("REPORT_SOURCE_AI_MODEL")
            or "gemini-1.5-flash-002"
        ).strip()
        self.deps_ready = bool(httpx is not None and google_auth_default is not None and Request is not None)

    def is_configured(self) -> bool:
        return bool(self.enabled and self.project and self.model and self.deps_ready)

    def extract(self, ticker: str, doc_meta: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None
        content = (text or "").strip()
        if not content:
            return None

        token = self._get_access_token()
        if not token:
            return None

        prompt = self._build_prompt(ticker=ticker, doc_meta=doc_meta, text=content[:18000])
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1},
        }

        api_host = os.environ.get("VERTEX_API_ENDPOINT") or f"{self.location}-aiplatform.googleapis.com"
        url = (
            f"https://{api_host}/v1/projects/{self.project}/locations/{self.location}/"
            f"publishers/google/models/{self.model}:generateContent"
        )
        try:
            res = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            if res.status_code >= 300:
                return None
            data = res.json()
            text_out = self._extract_text(data)
            if not text_out:
                return None
            obj = self._extract_json(text_out)
            if not isinstance(obj, dict):
                return None
            return obj
        except Exception:
            return None

    def _get_access_token(self) -> str:
        try:
            credentials, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(Request())
            return credentials.token or ""
        except Exception:
            return ""

    def _build_prompt(self, ticker: str, doc_meta: Dict[str, Any], text: str) -> str:
        doc_type = str(doc_meta.get("doc_type") or "")
        url = str(doc_meta.get("url") or "")
        title = str(doc_meta.get("title") or "")
        return (
            "You are a strict buy-side earnings analyst. "
            "Extract structured facts and explain causal chain.\n"
            "Return JSON only. No markdown.\n"
            "Schema:\n"
            "{"
            "\"summary\": string, "
            "\"causal_chain\": [{\"cause\": string, \"effect\": string, \"evidence\": string}], "
            "\"metrics\": {"
            "\"revenue\": {\"value\": number|null, \"unit\": string, \"evidence\": string}, "
            "\"eps\": {\"value\": number|null, \"unit\": string, \"evidence\": string}, "
            "\"guidance\": {\"text\": string, \"direction\": \"up|down|maintained|withdrawn|unknown\", \"evidence\": string}, "
            "\"capex\": {\"text\": string, \"direction\": \"up|down|flat|unknown\", \"evidence\": string}, "
            "\"segment_signals\": [{\"segment\": string, \"signal\": \"up|down|mixed\", \"evidence\": string}], "
            "\"one_off_items\": [{\"item\": string, \"impact\": \"positive|negative|neutral\", \"evidence\": string}], "
            "\"management_commentary\": [{\"topic\": string, \"stance\": \"bullish|cautious|neutral\", \"evidence\": string}]"
            "}, "
            "\"valuation_model_change\": {\"changed\": boolean, \"reason\": string, \"evidence\": string}, "
            "\"business_model_change\": {\"changed\": boolean, \"reason\": string, \"evidence\": string}, "
            "\"confidence\": number"
            "}\n"
            f"Ticker: {ticker}\n"
            f"Document type: {doc_type}\n"
            f"Document URL: {url}\n"
            f"Document title: {title}\n"
            f"Document text: {text}\n"
        )

    def _extract_text(self, data: Dict[str, Any]) -> str:
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            return ""
        for cand in candidates:
            parts = ((cand or {}).get("content") or {}).get("parts")
            if not isinstance(parts, list):
                continue
            chunks = [str(p.get("text")) for p in parts if isinstance(p, dict) and p.get("text")]
            if chunks:
                return "\n\n".join(chunks)
        return ""

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        cleaned = text.strip().replace("```json", "").replace("```", "").strip()
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        start = cleaned.find("{")
        if start < 0:
            return None
        for idx in range(len(cleaned) - 1, start, -1):
            if cleaned[idx] != "}":
                continue
            chunk = cleaned[start : idx + 1]
            try:
                parsed = json.loads(chunk)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                continue
        return None

