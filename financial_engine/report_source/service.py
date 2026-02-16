from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yfinance as yf

from .ai_verifier import VertexAIVerifier
from .fetcher import ReportSourceFetcher
from .models import CandidateScore, PageSnapshot, ReportSourceResult
from .storage import ReportSourceStorage

_BAD_HOST_KEYWORDS = {
    "seekingalpha.com",
    "investing.com",
    "marketwatch.com",
    "fool.com",
    "benzinga.com",
    "tipranks.com",
    "finance.yahoo.com",
    "sec.report",
    "stockanalysis.com",
    "nasdaq.com",
    "prnewswire.com",
    "quartr.com",
    "secfilings.com",
    "daloopa.com",
    "businesswire.com",
    "globenewswire.com",
}

_IR_KEYWORDS = [
    "investor relations",
    "investors",
    "financial results",
    "earnings",
    "annual report",
    "quarterly report",
    "shareholder",
]

_FINANCIAL_PAGE_KEYWORDS = [
    "financial results",
    "earnings release",
    "press release",
    "quarterly results",
    "annual results",
    "results center",
]

_SEC_PAGE_KEYWORDS = [
    "sec filings",
    "sec filing",
    "10-k",
    "10-q",
    "8-k",
]

_IR_HINTS = ["investor relations", "investors", "/investor", "/ir", "shareholder"]
_REPORT_HINTS = [
    "financial results",
    "earnings",
    "quarterly results",
    "annual report",
    "annual results",
    "/results",
    "/financial",
    "/earnings",
    "/quarterly",
    "/annual",
]
_SEC_HINTS = ["sec filings", "sec filing", "10-k", "10-q", "8-k", "/sec", "/filings", "edgar"]


class ReportSourceService:
    def __init__(
        self,
        bucket_name: str,
        local_data_dir: str,
        prefix: str = "report_sources",
        cache_ttl_seconds: int = 86400,
        max_candidates: int = 24,
    ) -> None:
        self.storage = ReportSourceStorage(bucket_name=bucket_name, local_data_dir=local_data_dir, prefix=prefix)
        self.fetcher = ReportSourceFetcher()
        self.ai_verifier = VertexAIVerifier()
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.max_candidates = max(8, int(max_candidates))

    def resolve(self, ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
        ticker_u = str(ticker or "").strip().upper()
        if not ticker_u:
            raise ValueError("ticker is required")

        if not force_refresh:
            cached = self.storage.load(ticker_u, max_age_seconds=self.cache_ttl_seconds)
            if isinstance(cached, dict):
                cached["cache"] = {"hit": True, "ttl_seconds": self.cache_ttl_seconds}
                return cached

        company_name, company_website = self._get_company_profile(ticker_u)
        website_domain = self._extract_domain(company_website)
        domain_tokens = self._extract_domain_tokens(website_domain)

        candidate_specs = self._build_candidates(ticker_u, company_name, company_website)
        snapshots = self._collect_snapshots(candidate_specs)
        scored = self._score_candidates(snapshots, company_name, website_domain, domain_tokens)

        if self.ai_verifier.is_configured() and scored:
            self._apply_ai_verification(ticker_u, company_name, scored)

        scored.sort(key=lambda x: x.score, reverse=True)

        result = self._build_result(
            ticker=ticker_u,
            company_name=company_name,
            company_website=company_website,
            scored=scored,
        )

        payload = result.to_dict()
        storage_paths = self.storage.save(ticker_u, payload)
        payload["storage"] = storage_paths
        payload["cache"] = {"hit": False, "ttl_seconds": self.cache_ttl_seconds}
        return payload

    def resolve_batch(self, tickers: List[str], force_refresh: bool = False) -> Dict[str, Any]:
        normalized = []
        for raw in tickers or []:
            t = str(raw or "").strip().upper()
            if t and t not in normalized:
                normalized.append(t)

        items: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        for t in normalized:
            try:
                items.append(self.resolve(t, force_refresh=force_refresh))
            except Exception as exc:
                failed.append({"ticker": t, "error": str(exc)})

        return {
            "count": len(normalized),
            "success": len(items),
            "failed": len(failed),
            "items": items,
            "errors": failed,
        }

    def list_catalog(self, limit: int = 500, ticker_prefix: str = "") -> Dict[str, Any]:
        rows = self.storage.list_records(limit=limit, ticker_prefix=ticker_prefix)
        items: List[Dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "ticker": row.get("ticker"),
                    "company_name": row.get("company_name"),
                    "company_website": row.get("company_website"),
                    "ir_home_url": row.get("ir_home_url"),
                    "financial_reports_url": row.get("financial_reports_url"),
                    "sec_filings_url": row.get("sec_filings_url"),
                    "confidence": row.get("confidence"),
                    "verification_status": row.get("verification_status"),
                    "discovered_at": row.get("discovered_at"),
                    "candidate_count": ((row.get("evidence") or {}).get("candidate_count") if isinstance(row.get("evidence"), dict) else None),
                }
            )
        return {
            "count": len(items),
            "limit": limit,
            "ticker_prefix": ticker_prefix or "",
            "items": items,
        }

    def _get_company_profile(self, ticker: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            info = yf.Ticker(ticker).info or {}
            company_name = str(info.get("longName") or info.get("shortName") or "").strip() or None
            website = str(info.get("website") or "").strip() or None
            return company_name, website
        except Exception:
            return None, None

    def _build_candidates(self, ticker: str, company_name: Optional[str], company_website: Optional[str]) -> List[Tuple[str, str]]:
        candidates: List[Tuple[str, str]] = []

        def add(url: Optional[str], source: str) -> None:
            if not url:
                return
            u = url.strip()
            if not u:
                return
            if not u.startswith("http"):
                u = f"https://{u.lstrip('/')}"
            candidates.append((u, source))

        website = (company_website or "").strip()
        domain = self._extract_domain(website)
        if website:
            add(website, "yfinance_website")

        if domain:
            add(f"https://investor.{domain}", "domain_pattern")
            add(f"https://investors.{domain}", "domain_pattern")
            add(f"https://ir.{domain}", "domain_pattern")
            add(f"https://www.{domain}/investor-relations", "domain_pattern")
            add(f"https://www.{domain}/investors", "domain_pattern")
            add(f"https://www.{domain}/investor", "domain_pattern")
            add(f"https://{domain}/investor-relations", "domain_pattern")
            add(f"https://{domain}/investors", "domain_pattern")

        q1 = f"{ticker} investor relations"
        q2 = f"{ticker} financial results investor relations"
        queries = [q1, q2]
        if company_name:
            queries.insert(0, f"{company_name} investor relations")

        for q in queries:
            for found in self.fetcher.search_candidates(q, limit=8):
                add(found, f"search:{q}")

        # Deduplicate while preserving priority order.
        seen = set()
        out: List[Tuple[str, str]] = []
        for url, source in candidates:
            key = url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append((url, source))
            if len(out) >= self.max_candidates:
                break
        return out

    def _collect_snapshots(self, candidates: List[Tuple[str, str]]) -> List[Tuple[str, PageSnapshot]]:
        snapshots: List[Tuple[str, PageSnapshot]] = []
        for url, source in candidates:
            snap = self.fetcher.fetch_page(url)
            if not snap:
                continue
            snapshots.append((source, snap))
        return snapshots

    def _score_candidates(
        self,
        snapshots: List[Tuple[str, PageSnapshot]],
        company_name: Optional[str],
        website_domain: Optional[str],
        domain_tokens: List[str],
    ) -> List[CandidateScore]:
        scored: List[CandidateScore] = []
        for source, snap in snapshots:
            score = 0.0
            matched: List[str] = []
            final_url = snap.final_url or snap.url
            host = (urlparse(final_url).hostname or "").lower()
            path_q = (urlparse(final_url).path or "") + " " + (urlparse(final_url).query or "")
            page_text = f"{snap.title} {snap.text}".lower()

            if 200 <= snap.status_code < 300:
                score += 12
            elif 300 <= snap.status_code < 400:
                score += 4
            else:
                score -= 20

            if "text/html" in (snap.content_type or "") or not snap.content_type:
                score += 2

            if website_domain and (host == website_domain or host.endswith(f".{website_domain}")):
                score += 20
                company_domain_match = True
            elif domain_tokens and any(tok and tok in host for tok in domain_tokens):
                # Allow sister domains like atmeta.com / aboutamazon.com
                score += 11
                company_domain_match = True
            else:
                company_domain_match = False

            for k in _IR_KEYWORDS:
                if k in page_text:
                    matched.append(k)
                    score += 6

            for k in _FINANCIAL_PAGE_KEYWORDS:
                if k in page_text:
                    matched.append(k)
                    score += 4

            for k in _SEC_PAGE_KEYWORDS:
                if k in page_text:
                    matched.append(k)
                    score += 4

            if any(seg in path_q.lower() for seg in ["investor", "investors", "ir", "financial-results", "earnings"]):
                score += 10

            # Some official investor sites are protected and may return 403/429 to bots.
            if snap.status_code in {403, 429} and any(seg in path_q.lower() for seg in ["investor", "investors", "ir"]):
                score += 8
            if snap.status_code in {403, 429} and company_domain_match and (
                host.startswith("ir.") or host.startswith("investor.") or host.startswith("investors.")
            ):
                score += 18

            if company_name:
                # Company-name evidence: if title includes one of the first two tokens.
                tokens = [t for t in re.split(r"[^a-z0-9]+", company_name.lower()) if t]
                if tokens:
                    token_hits = sum(1 for t in tokens[:2] if t and t in page_text)
                    score += token_hits * 3

            if any(bad in host for bad in _BAD_HOST_KEYWORDS):
                score -= 45

            if len(matched) > 8:
                matched = matched[:8]

            scored.append(
                CandidateScore(
                    url=snap.url,
                    final_url=final_url,
                    source=source,
                    score=round(score, 2),
                    matched_keywords=sorted(set(matched)),
                    status_code=snap.status_code,
                    title=snap.title,
                    snippet=(snap.text or "")[:300],
                    company_domain_match=company_domain_match,
                )
            )
        return scored

    def _apply_ai_verification(self, ticker: str, company_name: Optional[str], scored: List[CandidateScore]) -> None:
        # Only verify top candidates to keep latency/cost bounded.
        top = sorted(scored, key=lambda x: x.score, reverse=True)[:3]
        by_final = {c.final_url: c for c in scored}
        for c in top:
            snap = self.fetcher.fetch_page(c.final_url)
            if not snap:
                continue
            verdict = self.ai_verifier.verify(ticker=ticker, company_name=company_name, snapshot=snap)
            if not verdict:
                continue
            c.ai_verified = bool(verdict.get("is_official_ir_page"))
            c.ai_confidence = float(verdict.get("confidence", 0.0) or 0.0)
            c.ai_reason = str(verdict.get("reason") or "").strip()[:240]
            if c.ai_verified:
                c.score += 10.0 + 8.0 * c.ai_confidence
            elif c.ai_confidence >= 0.7:
                c.score -= 20.0
            # Keep same object in main list (by reference), but if copied ensure sync.
            by_final[c.final_url] = c

    def _build_result(
        self,
        ticker: str,
        company_name: Optional[str],
        company_website: Optional[str],
        scored: List[CandidateScore],
    ) -> ReportSourceResult:
        evidence = {
            "candidate_count": len(scored),
            "candidates": [c.to_dict() for c in scored[:12]],
            "ai_enabled": self.ai_verifier.is_configured(),
        }
        if not scored:
            return ReportSourceResult.not_found(ticker, company_name, company_website, evidence)

        ir = self._pick_best(scored, mode="ir")
        reports = self._pick_best(scored, mode="reports")
        sec = self._pick_best(scored, mode="sec")

        top_score = max(0.0, scored[0].score)
        confidence = max(0.0, min(top_score / 70.0, 1.0))

        status = "verified"
        if not ir:
            status = "not_found"
            confidence = 0.0
        elif not reports and not sec:
            status = "partial"
            confidence = min(confidence, 0.75)

        return ReportSourceResult(
            ticker=ticker,
            company_name=company_name,
            company_website=company_website,
            ir_home_url=ir,
            financial_reports_url=reports,
            sec_filings_url=sec,
            confidence=round(confidence, 3),
            verification_status=status,
            discovered_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            evidence=evidence,
        )

    def _pick_best(self, scored: List[CandidateScore], mode: str) -> Optional[str]:
        best_url: Optional[str] = None
        best_score = float("-inf")
        best_hard_signal = False
        for c in scored:
            parsed = urlparse(c.final_url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
            query = (parsed.query or "").lower()
            text = f"{c.title} {c.snippet} {path} {query}".lower()

            mode_bonus, hard_signal = self._mode_signal(mode=mode, host=host, path=path, text=text)
            is_home_like = self._is_home_like_path(path)

            # reports/sec 严格要求模式信号，避免把公司首页当成结果。
            if mode in {"reports", "sec"} and not hard_signal:
                continue
            if mode in {"reports", "sec"} and is_home_like and not self._has_mode_path_signal(mode, path):
                continue
            if mode == "reports" and not c.company_domain_match:
                continue
            if mode == "sec" and not c.company_domain_match and "sec.gov" not in host:
                continue

            # IR 不接受纯首页（例如 https://www.xxx.com 或 /en-us）且无 IR 证据。
            if mode == "ir" and is_home_like and not hard_signal:
                continue

            # 即使有轻微信号，home-like URL 仍应额外降权。
            home_penalty = 14.0 if is_home_like else 0.0
            if mode == "ir" and (host.startswith("ir.") or host.startswith("investor.") or host.startswith("investors.")):
                # IR home pages often live at "/" on dedicated IR subdomains.
                home_penalty = 0.0
            final = c.score + mode_bonus - home_penalty

            # Prefer official company domains. SEC mode may allow sec.gov.
            if mode == "sec":
                if not c.company_domain_match and "sec.gov" not in host:
                    final -= 18.0
            else:
                if not c.company_domain_match:
                    final -= 20.0

            if final > best_score:
                best_score = final
                best_url = c.final_url
                best_hard_signal = hard_signal

        min_score = 18.0 if (mode == "ir" and best_hard_signal) else (24.0 if mode == "ir" else 30.0)
        if best_score < min_score:
            return None
        return best_url

    def _mode_signal(self, mode: str, host: str, path: str, text: str) -> Tuple[float, bool]:
        bonus = 0.0
        hard = False

        if mode == "ir":
            if host.startswith("investor.") or host.startswith("investors.") or host.startswith("ir."):
                bonus += 10.0
                hard = True
            if any(k in text for k in _IR_HINTS):
                bonus += 9.0
                hard = True
            return bonus, hard

        if mode == "reports":
            path_hit = any(k in path for k in ["/results", "/financial", "/earnings", "/quarterly", "/annual", "/reports"])
            text_hit = any(k in text for k in _REPORT_HINTS)
            if path_hit:
                bonus += 12.0
                hard = True
            if text_hit:
                bonus += 8.0
                hard = True
            return bonus, hard

        if mode == "sec":
            path_hit = any(k in path for k in ["/sec", "/filings", "/governance", "/investor"])
            text_hit = any(k in text for k in _SEC_HINTS)
            if "sec.gov" in host:
                bonus += 8.0
                hard = True
            if path_hit:
                bonus += 10.0
            if text_hit:
                bonus += 9.0
                hard = True
            return bonus, hard

        return bonus, hard

    def _is_home_like_path(self, path: str) -> bool:
        p = (path or "").strip().lower()
        if not p or p == "/":
            return True
        # Locale-only path, e.g. /en-us or /zh-cn/
        if re.fullmatch(r"/[a-z]{2}(?:-[a-z]{2})?/?", p):
            return True
        # Generic home/index/default entries.
        if re.fullmatch(r"/(?:home|index|default|default\\.aspx)/?", p):
            return True
        return False

    def _has_mode_path_signal(self, mode: str, path: str) -> bool:
        p = (path or "").lower()
        if mode == "reports":
            return any(k in p for k in ["/results", "/financial", "/earnings", "/quarterly", "/annual", "/reports"])
        if mode == "sec":
            return any(k in p for k in ["/sec", "/filings", "/governance", "/edgar", "/10-k", "/10-q"])
        return any(k in p for k in ["/investor", "/investors", "/ir"])

    def _extract_domain(self, website: Optional[str]) -> Optional[str]:
        if not website:
            return None
        raw = website.strip()
        if not raw:
            return None
        if not raw.startswith("http"):
            raw = "https://" + raw
        try:
            host = (urlparse(raw).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host or None
        except Exception:
            return None

    def _extract_domain_tokens(self, domain: Optional[str]) -> List[str]:
        if not domain:
            return []
        host = domain.lower().strip().strip(".")
        if not host:
            return []
        labels = [p for p in host.split(".") if p]
        if not labels:
            return []

        first = labels[0]
        tokens = [first]
        # aboutamazon.com -> amazon, atmeta.com -> meta
        if first.startswith("about") and len(first) > len("about"):
            tokens.append(first[len("about") :])
        if first.startswith("at") and len(first) > len("at"):
            tokens.append(first[len("at") :])
        return [t for t in tokens if len(t) >= 3]
