from __future__ import annotations

import logging
import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

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
    def __init__(self, timeout_seconds: float = 8.0, max_html_chars: int = 300_000, max_text_chars: int = 20_000) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_html_chars = max_html_chars
        self.max_text_chars = max_text_chars
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

    def close(self) -> None:
        self._client.close()

    def fetch_page(self, url: str) -> PageSnapshot | None:
        try:
            response = self._client.get(url)
        except Exception:
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        body = ""
        if "text/html" in content_type or "application/xhtml+xml" in content_type or not content_type:
            body = response.text[: self.max_html_chars]

        title = _extract_title(body) if body else ""
        text = _extract_text(body)[: self.max_text_chars] if body else ""

        links: List[str] = []
        if body:
            parser = _LinkExtractor()
            try:
                parser.feed(body)
                links = parser.links
            except Exception:
                links = []

        return PageSnapshot(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            content_type=content_type,
            title=title,
            text=text,
            links=links,
        )

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
