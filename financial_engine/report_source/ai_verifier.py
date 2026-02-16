from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

try:
    import httpx
except Exception:  # pragma: no cover - optional dependency fallback
    httpx = None

try:
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request
except Exception:  # pragma: no cover - optional dependency fallback
    google_auth_default = None
    Request = None

from .models import PageSnapshot


class VertexAIVerifier:
    def __init__(self) -> None:
        self.enabled = str(os.environ.get("REPORT_SOURCE_ENABLE_AI", "0")).strip().lower() in {"1", "true", "yes"}
        self.project = (
            os.environ.get("VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or ""
        ).strip()
        self.location = (os.environ.get("VERTEX_LOCATION") or "us-central1").strip()
        self.model = (os.environ.get("REPORT_SOURCE_AI_MODEL") or "gemini-1.5-flash-002").strip()
        self.deps_ready = bool(httpx is not None and google_auth_default is not None and Request is not None)

    def is_configured(self) -> bool:
        return bool(self.enabled and self.project and self.model and self.deps_ready)

    def verify(self, ticker: str, company_name: Optional[str], snapshot: PageSnapshot) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None

        token = self._get_access_token()
        if not token:
            return None

        prompt = self._build_prompt(ticker, company_name, snapshot)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.0},
        }

        api_host = os.environ.get("VERTEX_API_ENDPOINT") or f"{self.location}-aiplatform.googleapis.com"
        url = (
            f"https://{api_host}/v1/projects/{self.project}/locations/{self.location}/"
            f"publishers/google/models/{self.model}:generateContent"
        )

        try:
            res = httpx.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
                timeout=20.0,
            )
            if res.status_code >= 300:
                return None
            data = res.json()
            text = self._extract_text(data)
            if not text:
                return None
            parsed = self._extract_json(text)
            if not isinstance(parsed, dict):
                return None
            is_official = bool(parsed.get("is_official_ir_page", False))
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
            reason = str(parsed.get("reason", "")).strip()
            page_kind = str(parsed.get("page_kind", "unknown")).strip().lower() or "unknown"
            return {
                "is_official_ir_page": is_official,
                "confidence": max(0.0, min(confidence, 1.0)),
                "reason": reason,
                "page_kind": page_kind,
            }
        except Exception:
            return None

    def _get_access_token(self) -> str:
        try:
            credentials, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(Request())
            return credentials.token or ""
        except Exception:
            return ""

    def _build_prompt(self, ticker: str, company_name: Optional[str], snapshot: PageSnapshot) -> str:
        snippet = (snapshot.text or "")[:2500]
        title = snapshot.title or ""
        return (
            "You are a strict verifier for U.S. listed company investor-relations pages.\n"
            "Decide if the page is an official company investor-relations/financial-reports page.\n"
            "Return JSON only with schema: "
            "{\"is_official_ir_page\": boolean, \"confidence\": number, \"reason\": string, \"page_kind\": "
            "\"ir_home\"|\"financial_results\"|\"sec_filings\"|\"other\"}.\n"
            "Do not return markdown.\n"
            f"Ticker: {ticker}\n"
            f"Company: {company_name or ''}\n"
            f"Requested URL: {snapshot.url}\n"
            f"Final URL: {snapshot.final_url}\n"
            f"HTML title: {title}\n"
            f"Page snippet: {snippet}\n"
        )

    def _extract_text(self, data: Dict[str, Any]) -> str:
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            return ""
        for cand in candidates:
            parts = ((cand or {}).get("content") or {}).get("parts")
            if not isinstance(parts, list):
                continue
            text_parts = [str(p.get("text")) for p in parts if isinstance(p, dict) and p.get("text")]
            if text_parts:
                return "\n\n".join(text_parts)
        return ""

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        cleaned = text.strip().replace("```json", "").replace("```", "").strip()
        try:
            obj = json.loads(cleaned)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
        start = cleaned.find("{")
        if start < 0:
            return None
        for i in range(len(cleaned) - 1, start, -1):
            if cleaned[i] != "}":
                continue
            chunk = cleaned[start : i + 1]
            try:
                obj = json.loads(chunk)
                return obj if isinstance(obj, dict) else None
            except Exception:
                continue
        return None
