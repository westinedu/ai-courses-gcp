from __future__ import annotations

import base64
import logging
import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover
    cloudscraper = None  # type: ignore[assignment]

from .models import PageSnapshot

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def _resolve_httpx_log_level() -> int:
    raw = str(os.environ.get("REPORT_SOURCE_HTTPX_LOG_LEVEL", "")).strip().upper()
    if not raw:
        # Keep historical behavior: show per-request INFO logs by default.
        return logging.INFO
    return getattr(logging, raw, logging.INFO)


logging.getLogger("httpx").setLevel(_resolve_httpx_log_level())
logger = logging.getLogger(__name__)

_CHALLENGE_MARKERS = [
    "just a moment",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "verify you are human",
    "are you human",
    "captcha",
    "cloudflare",
    "cf-chl",
]


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.links.append(v.strip())


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return unescape(re.sub(r"\s+", " ", m.group(1))).strip()


def _extract_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_ddg_url(href: str) -> str:
    href = unescape((href or "").strip())
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    if "duckduckgo.com/l/?" not in href:
        return href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg")
    if not uddg:
        return href
    return unescape(uddg[0]).strip()


class ReportSourceFetcher:
    def __init__(
        self,
        timeout_seconds: float = 8.0,
        max_html_chars: int = 300_000,
        max_text_chars: int = 20_000,
        max_raw_bytes: int = 5_000_000,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_html_chars = max_html_chars
        self.max_text_chars = max_text_chars
        self.max_raw_bytes = max(100_000, int(max_raw_bytes))
        self.enable_challenge_bypass = _parse_bool_env("REPORT_SOURCE_ENABLE_CHALLENGE_BYPASS", True)
        self.enable_cloudscraper_bypass = _parse_bool_env("REPORT_SOURCE_ENABLE_CLOUDSCRAPER_BYPASS", True)
        self.fetch_agent_url = str(os.environ.get("REPORT_SOURCE_FETCH_AGENT_URL", "") or "").strip()
        self.fetch_agent_timeout_seconds = max(
            2.0,
            float(os.environ.get("REPORT_SOURCE_FETCH_AGENT_TIMEOUT_SECONDS", "20") or 20),
        )
        self.google_api_key = (
            os.environ.get("REPORT_SOURCE_GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_SEARCH_API_KEY")
            or ""
        ).strip()
        self.google_cx = (
            os.environ.get("REPORT_SOURCE_GOOGLE_CX")
            or os.environ.get("GOOGLE_SEARCH_CX")
            or ""
        ).strip()
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        self._cloudscraper_client = None
        if self.enable_challenge_bypass and self.enable_cloudscraper_bypass and cloudscraper is not None:
            try:
                self._cloudscraper_client = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "darwin", "mobile": False}
                )
            except Exception:
                self._cloudscraper_client = None

    def close(self) -> None:
        self._client.close()
        cs = self._cloudscraper_client
        if cs is not None:
            try:
                cs.close()
            except Exception:
                pass

    def _build_snapshot(
        self,
        *,
        url: str,
        final_url: str,
        status_code: int,
        content_type: str,
        response_text: str,
        response_bytes: bytes,
        fetch_via: str,
        include_raw: bool,
        links_override: Optional[List[str]] = None,
        title_override: Optional[str] = None,
    ) -> PageSnapshot:
        content_type_norm = (content_type or "").lower()
        body = ""
        if "text/html" in content_type_norm or "application/xhtml+xml" in content_type_norm or not content_type_norm:
            body = str(response_text or "")[: self.max_html_chars]
        title = str(title_override or "").strip()
        if not title and body:
            title = _extract_title(body)
        text = _extract_text(body)[: self.max_text_chars] if body else str(response_text or "")[: self.max_text_chars]

        links: List[str] = []
        if isinstance(links_override, list):
            links = [str(x) for x in links_override if str(x or "").strip()]
        elif body:
            parser = _LinkExtractor()
            try:
                parser.feed(body)
                links = parser.links
            except Exception:
                links = []

        raw_bytes = b""
        if include_raw:
            raw_bytes = bytes((response_bytes or b"")[: self.max_raw_bytes])
            if not raw_bytes and response_text:
                raw_bytes = response_text.encode("utf-8", errors="ignore")[: self.max_raw_bytes]

        return PageSnapshot(
            url=url,
            final_url=final_url,
            status_code=int(status_code),
            content_type=content_type_norm,
            title=title,
            text=text,
            links=links,
            raw_bytes=raw_bytes,
            fetch_via=fetch_via,
        )

    @staticmethod
    def _is_challenge_snapshot(snap: PageSnapshot) -> bool:
        text_block = f"{snap.title} {snap.text}".lower()
        if any(marker in text_block for marker in _CHALLENGE_MARKERS):
            return True
        return int(snap.status_code or 0) in {403, 429, 503} and "access denied" in text_block

    def _fetch_via_httpx(self, url: str, *, include_raw: bool) -> Optional[PageSnapshot]:
        try:
            response = self._client.get(url)
        except Exception:
            return None
        try:
            response_text = response.text
        except Exception:
            response_text = ""
        try:
            response_bytes = bytes(response.content or b"")
        except Exception:
            response_bytes = b""
        return self._build_snapshot(
            url=url,
            final_url=str(response.url),
            status_code=int(response.status_code or 0),
            content_type=str(response.headers.get("content-type") or ""),
            response_text=response_text,
            response_bytes=response_bytes,
            fetch_via="httpx",
            include_raw=include_raw,
        )

    def _fetch_via_cloudscraper(self, url: str, *, include_raw: bool) -> Optional[PageSnapshot]:
        if self._cloudscraper_client is None:
            return None
        try:
            response = self._cloudscraper_client.get(url, timeout=self.timeout_seconds)
        except Exception:
            return None
        try:
            response_text = response.text
        except Exception:
            response_text = ""
        try:
            response_bytes = bytes(response.content or b"")
        except Exception:
            response_bytes = b""
        final_url = str(getattr(response, "url", "") or url)
        status_code = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", {}) or {}
        content_type = str((headers.get("content-type") if hasattr(headers, "get") else "") or "")
        return self._build_snapshot(
            url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            response_text=response_text,
            response_bytes=response_bytes,
            fetch_via="cloudscraper",
            include_raw=include_raw,
        )

    def _fetch_via_agent(self, url: str, *, include_raw: bool) -> Optional[PageSnapshot]:
        if not self.fetch_agent_url:
            return None
        payload = {
            "url": url,
            "include_raw": bool(include_raw),
            "max_html_chars": int(self.max_html_chars),
            "max_text_chars": int(self.max_text_chars),
            "max_raw_bytes": int(self.max_raw_bytes),
        }
        try:
            res = self._client.post(self.fetch_agent_url, json=payload, timeout=self.fetch_agent_timeout_seconds)
        except Exception:
            return None
        if int(res.status_code or 0) >= 300:
            return None
        try:
            data = res.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        final_url = str(data.get("final_url") or data.get("url") or url)
        status_code = int(data.get("status_code") or 0)
        content_type = str(data.get("content_type") or data.get("mime_type") or "")
        title = str(data.get("title") or "")
        response_text = str(data.get("html") or data.get("text") or data.get("body_text") or "")

        raw_bytes = b""
        raw_b64 = data.get("raw_b64")
        if isinstance(raw_b64, str) and raw_b64.strip():
            try:
                raw_bytes = base64.b64decode(raw_b64.encode("utf-8", errors="ignore"), validate=False)
            except Exception:
                raw_bytes = b""
        if not raw_bytes:
            raw_text = str(data.get("raw_text") or "")
            if raw_text:
                raw_bytes = raw_text.encode("utf-8", errors="ignore")

        links = data.get("links")
        links_override = links if isinstance(links, list) else None
        return self._build_snapshot(
            url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            response_text=response_text,
            response_bytes=raw_bytes,
            fetch_via="agent",
            include_raw=include_raw,
            links_override=links_override,
            title_override=title,
        )

    def fetch_page(self, url: str, *, include_raw: bool = False) -> PageSnapshot | None:
        primary = self._fetch_via_httpx(url, include_raw=include_raw)
        if primary is None:
            if self.enable_challenge_bypass:
                fallback = self._fetch_via_agent(url, include_raw=include_raw)
                if fallback is not None:
                    return fallback
            return None

        if not self.enable_challenge_bypass or not self._is_challenge_snapshot(primary):
            return primary

        candidates: List[PageSnapshot] = [primary]
        cloud = self._fetch_via_cloudscraper(url, include_raw=include_raw)
        if cloud is not None:
            candidates.append(cloud)
            if not self._is_challenge_snapshot(cloud):
                logger.info("report_source fetch bypass success via=cloudscraper url=%s status=%s", url, cloud.status_code)
                return cloud

        agent = self._fetch_via_agent(url, include_raw=include_raw)
        if agent is not None:
            candidates.append(agent)
            if not self._is_challenge_snapshot(agent):
                logger.info("report_source fetch bypass success via=agent url=%s status=%s", url, agent.status_code)
                return agent

        candidates.sort(
            key=lambda s: (
                0 if self._is_challenge_snapshot(s) else 1,
                len(s.text or ""),
                int(s.status_code or 0),
            ),
            reverse=True,
        )
        best = candidates[0]
        logger.info(
            "report_source fetch challenge unresolved url=%s via=%s status=%s text_len=%s",
            url,
            best.fetch_via,
            best.status_code,
            len(best.text or ""),
        )
        return best

    def search_candidates(self, query: str, limit: int = 10) -> List[str]:
        target = max(1, int(limit))
        merged: List[str] = []
        seen = set()

        # 1) Optional Google Programmable Search (higher precision if configured).
        if self.google_api_key and self.google_cx:
            for link in self._search_candidates_google(query=query, limit=min(10, max(target, 6))):
                key = link.lower().rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(link)
                if len(merged) >= target:
                    return merged

        # 2) DuckDuckGo HTML endpoint as default/fallback.
        for link in self._search_candidates_ddg(query=query, limit=max(target, 8)):
            key = link.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            merged.append(link)
            if len(merged) >= target:
                break
        return merged

    def _search_candidates_ddg(self, query: str, limit: int = 10) -> List[str]:
        # DuckDuckGo HTML endpoint is a lightweight fallback and needs no API key.
        out: List[str] = []
        seen = set()
        search_urls = [
            f"https://duckduckgo.com/html/?q={quote_plus(query)}",
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        ]
        for search_url in search_urls:
            try:
                res = self._client.get(search_url)
            except Exception:
                continue
            if res.status_code >= 400:
                continue

            raw = res.text
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw, flags=re.IGNORECASE)
            for href in hrefs:
                link = _normalize_ddg_url(href)
                if link.startswith("//"):
                    link = f"https:{link}"
                if not link or link.startswith("/"):
                    continue
                if not link.startswith("http"):
                    continue
                if "duckduckgo.com" in link:
                    continue
                key = link.lower().rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                out.append(link)
                if len(out) >= limit:
                    return out
        return out

    def _search_candidates_google(self, query: str, limit: int = 10) -> List[str]:
        if not self.google_api_key or not self.google_cx:
            return []
        url = "https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "key": self.google_api_key,
            "cx": self.google_cx,
            "q": query,
            "num": max(1, min(int(limit), 10)),
            "safe": "off",
        }
        try:
            res = self._client.get(url, params=params)
        except Exception:
            return []
        if res.status_code >= 400:
            return []

        try:
            data = res.json()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []

        out: List[str] = []
        seen = set()
        items = data.get("items")
        if not isinstance(items, list):
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            link = str(item.get("link") or "").strip()
            if not link or not link.startswith("http"):
                continue
            host = (urlparse(link).hostname or "").lower()
            if not host:
                continue
            if host.endswith("google.com") or host.endswith("gstatic.com"):
                continue
            key = link.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
            if len(out) >= limit:
                break
        return out
