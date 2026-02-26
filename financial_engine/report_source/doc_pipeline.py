from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .service import ReportSourceService
from .structured_extractor import VertexAIStructuredExtractor

logger = logging.getLogger(__name__)

router = APIRouter()

_DOC_PIPELINE: Optional["ReportSourceDocumentPipeline"] = None
_ROUTER_REGISTERED = False


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm_ticker(raw: str) -> str:
    return str(raw or "").strip().upper()


def _norm_tickers(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items or []:
        t = _norm_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _safe_json_dump(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _canonicalize_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    if parsed.scheme not in {"http", "https"}:
        return ""
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    # keep query for SEC URLs where filings are query-driven
    query = parsed.query or ""
    normalized = parsed._replace(netloc=netloc, path=path, fragment="")
    if not query:
        normalized = normalized._replace(query="")
    return urlunparse(normalized).rstrip("/")


def _doc_kind_from_url(url: str) -> Tuple[str, str, int]:
    u = str(url or "").lower()
    # authoritative filings
    if re.search(r"(^|[^a-z0-9])10[-_ ]?q([^a-z0-9]|$)", u):
        return "10-Q", "authoritative", 10
    if re.search(r"(^|[^a-z0-9])10[-_ ]?k([^a-z0-9]|$)", u):
        return "10-K", "authoritative", 10
    # fast path
    if re.search(r"(^|[^a-z0-9])8[-_ ]?k([^a-z0-9]|$)", u):
        return "8-K", "fast", 20
    if any(k in u for k in ["press-release", "news-release", "financial-results", "earnings-release", "quarterly-results"]):
        return "PR", "fast", 20
    # supplementary
    if any(k in u for k in ["transcript", "conference-call", "earnings-call"]):
        return "transcript", "supplementary", 40
    if any(k in u for k in ["presentation", "investor-day", "slides"]):
        return "presentation", "supplementary", 50
    # generic filings path
    if any(k in u for k in ["sec-filings", "filings", "edgar"]):
        return "filings_index", "supplementary", 55
    # only keep some obvious docs
    if u.endswith(".pdf"):
        return "pdf_document", "supplementary", 60
    return "", "", 0


def _extract_sentences(text: str) -> List[str]:
    raw = re.split(r"(?<=[\.\!\?])\s+|\n+", text or "")
    out = []
    for item in raw:
        s = str(item or "").strip()
        if len(s) < 30:
            continue
        out.append(s)
    return out


def _looks_like_access_challenge(title: str, text: str) -> bool:
    t = f"{title} {text}".lower()
    markers = [
        "just a moment",
        "enable javascript and cookies to continue",
        "checking your browser before accessing",
        "verify you are human",
        "captcha",
        "access denied",
    ]
    return any(m in t for m in markers)


def _extract_metric_sentence(text: str, keywords: List[str]) -> str:
    lower = (text or "").lower()
    if not lower:
        return ""
    for sent in _extract_sentences(text):
        ls = sent.lower()
        if any(k in ls for k in keywords):
            return sent[:420]
    return ""


def _extract_first_number(sentence: str) -> Optional[float]:
    if not sentence:
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)", sentence.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _guess_unit(sentence: str) -> str:
    s = (sentence or "").lower()
    if "%" in s:
        return "%"
    if "billion" in s or " bn" in s or s.endswith("b"):
        return "USD_billion"
    if "million" in s or " mm" in s or s.endswith("m"):
        return "USD_million"
    if "$" in s:
        return "USD"
    return "unknown"


def _direction_from_sentence(sentence: str) -> str:
    s = (sentence or "").lower()
    if any(k in s for k in ["increase", "increased", "up", "higher", "raised", "grew", "growth"]):
        return "up"
    if any(k in s for k in ["decrease", "decreased", "down", "lower", "cut", "declined"]):
        return "down"
    if any(k in s for k in ["maintain", "maintained", "unchanged", "flat"]):
        return "maintained"
    return "unknown"


def _heuristic_deep_analysis(ticker: str, doc_item: Dict[str, Any], title: str, text: str) -> Dict[str, Any]:
    revenue_sent = _extract_metric_sentence(text, ["revenue", "sales"])
    eps_sent = _extract_metric_sentence(text, ["eps", "earnings per share", "diluted"])
    guidance_sent = _extract_metric_sentence(text, ["guidance", "outlook", "forecast", "expects"])
    capex_sent = _extract_metric_sentence(text, ["capital expenditure", "capex", "infrastructure investment"])

    segment_terms = ["segment", "data center", "gaming", "automotive", "cloud", "enterprise", "consumer"]
    segment_sentences = []
    for sent in _extract_sentences(text):
        ls = sent.lower()
        if any(k in ls for k in segment_terms):
            segment_sentences.append(sent[:420])
        if len(segment_sentences) >= 4:
            break

    one_off_terms = ["one-time", "one time", "non-recurring", "restructuring", "impairment", "charge", "tax benefit", "write-down"]
    one_off_sentences = []
    for sent in _extract_sentences(text):
        ls = sent.lower()
        if any(k in ls for k in one_off_terms):
            one_off_sentences.append(sent[:420])
        if len(one_off_sentences) >= 3:
            break

    causal_chain = []
    for sent in _extract_sentences(text):
        ls = sent.lower()
        if any(k in ls for k in ["due to", "driven by", "because", "primarily", "as a result"]):
            causal_chain.append(
                {
                    "cause": "",
                    "effect": "",
                    "evidence": sent[:420],
                }
            )
        if len(causal_chain) >= 5:
            break

    valuation_change_evidence = _extract_metric_sentence(
        text,
        ["valuation", "multiple", "margin expansion", "discount rate", "wacc", "rerating", "re-rating"],
    )
    business_model_change_evidence = _extract_metric_sentence(
        text,
        ["business model", "subscription", "recurring", "pricing model", "mix shift", "platform"],
    )

    confidence = 0.35
    evidence_hits = 0
    for v in [revenue_sent, eps_sent, guidance_sent, capex_sent]:
        if v:
            evidence_hits += 1
    confidence += min(0.45, evidence_hits * 0.1)
    if causal_chain:
        confidence += 0.1
    confidence = max(0.05, min(confidence, 0.95))

    return {
        "schema_version": "report_source_doc_analysis_v1",
        "mode": "heuristic",
        "ticker": ticker,
        "doc_type": doc_item.get("doc_type"),
        "document_title": title,
        "summary": guidance_sent or revenue_sent or eps_sent or (text[:300] if text else ""),
        "causal_chain": causal_chain,
        "metrics": {
            "revenue": {
                "value": _extract_first_number(revenue_sent),
                "unit": _guess_unit(revenue_sent),
                "evidence": revenue_sent,
            },
            "eps": {
                "value": _extract_first_number(eps_sent),
                "unit": _guess_unit(eps_sent),
                "evidence": eps_sent,
            },
            "guidance": {
                "text": guidance_sent,
                "direction": _direction_from_sentence(guidance_sent),
                "evidence": guidance_sent,
            },
            "capex": {
                "text": capex_sent,
                "direction": _direction_from_sentence(capex_sent),
                "evidence": capex_sent,
            },
            "segment_signals": [{"segment": "", "signal": "mixed", "evidence": s} for s in segment_sentences],
            "one_off_items": [{"item": "", "impact": "neutral", "evidence": s} for s in one_off_sentences],
            "management_commentary": [],
        },
        "valuation_model_change": {
            "changed": bool(valuation_change_evidence),
            "reason": "detected valuation-related language in document" if valuation_change_evidence else "",
            "evidence": valuation_change_evidence,
        },
        "business_model_change": {
            "changed": bool(business_model_change_evidence),
            "reason": "detected business-model-related language in document" if business_model_change_evidence else "",
            "evidence": business_model_change_evidence,
        },
        "confidence": round(float(confidence), 3),
    }


class DiscoverDocsRequest(BaseModel):
    tickers: List[str] = Field(default_factory=list)
    force_source_refresh: bool = False
    max_links_per_source: int = Field(default=120, ge=20, le=600)


class AnalyzeDocRequest(BaseModel):
    use_ai: bool = True


class ReportSourceDocumentPipeline:
    def __init__(
        self,
        *,
        get_report_source_service: Callable[[], ReportSourceService],
        state_file_path: str,
        artifact_dir: str,
    ) -> None:
        self._get_report_source_service = get_report_source_service
        self._state_path = Path(state_file_path).resolve()
        self._artifact_dir = Path(artifact_dir).resolve()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []
        self._extractor = VertexAIStructuredExtractor()
        self._load_state()

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
        items = payload.get("items")
        events = payload.get("events")
        if isinstance(items, dict):
            self._items = {str(k): dict(v) for k, v in items.items() if isinstance(v, dict)}
        if isinstance(events, list):
            self._events = [dict(e) for e in events if isinstance(e, dict)][-500:]

    def _persist_state(self) -> None:
        payload = {
            "items": self._items,
            "events": self._events[-500:],
            "saved_at": _utc_iso_now(),
        }
        _safe_json_dump(self._state_path, payload)

    @staticmethod
    def _doc_id(ticker: str, canonical_url: str) -> str:
        raw = f"{ticker}|{canonical_url}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]

    def _collect_candidates_from_source(
        self,
        *,
        ticker: str,
        source_kind: str,
        source_url: str,
        links: List[str],
        max_links_per_source: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen = set()
        for raw in links[:max_links_per_source]:
            absolute = urljoin(source_url, str(raw or "").strip())
            canonical = _canonicalize_url(absolute)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            doc_type, lane, priority = _doc_kind_from_url(canonical)
            if not doc_type:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "source_kind": source_kind,
                    "source_url": source_url,
                    "url": absolute,
                    "canonical_url": canonical,
                    "doc_type": doc_type,
                    "lane": lane,
                    "priority": priority,
                }
            )
        return out

    def discover_documents(
        self,
        tickers: Optional[List[str]] = None,
        *,
        force_source_refresh: bool = False,
        max_links_per_source: int = 120,
    ) -> Dict[str, Any]:
        service = self._get_report_source_service()
        normalized_tickers = _norm_tickers(tickers or [])
        if not normalized_tickers:
            catalog = service.list_catalog(limit=1000, ticker_prefix="")
            normalized_tickers = _norm_tickers([str(it.get("ticker") or "") for it in catalog.get("items", []) if isinstance(it, dict)])

        discovered = 0
        updated = 0
        scanned_sources = 0
        scanned_links = 0
        now_iso = _utc_iso_now()

        for ticker in normalized_tickers:
            source_payload = None
            if force_source_refresh:
                try:
                    source_payload = service.resolve(ticker, force_refresh=True)
                except Exception as exc:
                    logger.warning("doc discover source refresh failed ticker=%s error=%s", ticker, exc)
            if not isinstance(source_payload, dict):
                source_payload = service.storage.load(ticker, 0)
            if not isinstance(source_payload, dict):
                continue

            source_urls = [
                ("ir_home", str(source_payload.get("ir_home_url") or "").strip()),
                ("financial_reports", str(source_payload.get("financial_reports_url") or "").strip()),
                ("sec_filings", str(source_payload.get("sec_filings_url") or "").strip()),
            ]
            for source_kind, source_url in source_urls:
                if not source_url:
                    continue
                scanned_sources += 1
                snap = service.fetcher.fetch_page(source_url)
                if not snap:
                    continue
                base_url = str(snap.final_url or source_url)
                raw_links = list(snap.links or [])
                # include source url itself for document-like direct pages
                raw_links.append(base_url)
                candidates = self._collect_candidates_from_source(
                    ticker=ticker,
                    source_kind=source_kind,
                    source_url=base_url,
                    links=raw_links,
                    max_links_per_source=max_links_per_source,
                )
                scanned_links += len(raw_links)
                with self._lock:
                    for cand in candidates:
                        doc_id = self._doc_id(cand["ticker"], cand["canonical_url"])
                        existing = self._items.get(doc_id)
                        if not isinstance(existing, dict):
                            self._items[doc_id] = {
                                "doc_id": doc_id,
                                "ticker": cand["ticker"],
                                "doc_type": cand["doc_type"],
                                "lane": cand["lane"],
                                "priority": cand["priority"],
                                "url": cand["url"],
                                "canonical_url": cand["canonical_url"],
                                "source_kind": cand["source_kind"],
                                "source_url": cand["source_url"],
                                "status": "queued",
                                "attempts": 0,
                                "last_error": "",
                                "first_seen_at": now_iso,
                                "last_seen_at": now_iso,
                                "discovered_at": now_iso,
                            }
                            discovered += 1
                        else:
                            existing["last_seen_at"] = now_iso
                            if existing.get("status") == "failed":
                                existing["status"] = "queued"
                            updated += 1

        with self._lock:
            self._events.append(
                {
                    "type": "discover_run",
                    "at": now_iso,
                    "tickers": len(normalized_tickers),
                    "scanned_sources": scanned_sources,
                    "scanned_links": scanned_links,
                    "discovered": discovered,
                    "updated": updated,
                }
            )
            self._events = self._events[-500:]
            self._persist_state()

        return {
            "ok": True,
            "at": now_iso,
            "tickers": len(normalized_tickers),
            "scanned_sources": scanned_sources,
            "scanned_links": scanned_links,
            "discovered": discovered,
            "updated": updated,
            "queue": self.status(),
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            items = list(self._items.values())
            by_status: Dict[str, int] = {}
            by_doc_type: Dict[str, int] = {}
            by_lane: Dict[str, int] = {}
            for item in items:
                st = str(item.get("status") or "unknown")
                dt = str(item.get("doc_type") or "unknown")
                ln = str(item.get("lane") or "unknown")
                by_status[st] = by_status.get(st, 0) + 1
                by_doc_type[dt] = by_doc_type.get(dt, 0) + 1
                by_lane[ln] = by_lane.get(ln, 0) + 1
            return {
                "items": len(items),
                "by_status": by_status,
                "by_doc_type": by_doc_type,
                "by_lane": by_lane,
                "ai_extractor_configured": self._extractor.is_configured(),
                "state_file": str(self._state_path),
                "artifact_dir": str(self._artifact_dir),
                "recent_events": self._events[-30:],
            }

    def list_items(self, *, status: str = "", ticker: str = "", limit: int = 100) -> Dict[str, Any]:
        st = str(status or "").strip().lower()
        tk = _norm_ticker(ticker or "")
        with self._lock:
            rows = list(self._items.values())
        filtered = []
        for row in rows:
            row_st = str(row.get("status") or "").lower()
            row_tk = _norm_ticker(str(row.get("ticker") or ""))
            if st and row_st != st:
                continue
            if tk and row_tk != tk:
                continue
            filtered.append(row)
        filtered.sort(key=lambda x: (int(x.get("priority") or 999), str(x.get("discovered_at") or "")))
        return {
            "count": len(filtered),
            "status": st or "",
            "ticker": tk or "",
            "items": filtered[: max(1, min(limit, 500))],
        }

    def get_item(self, doc_id: str) -> Dict[str, Any]:
        did = str(doc_id or "").strip()
        if not did:
            raise KeyError("doc_id is required")
        with self._lock:
            item = self._items.get(did)
        if not isinstance(item, dict):
            raise KeyError(f"doc_id not found: {did}")
        return dict(item)

    def get_analysis(self, doc_id: str) -> Dict[str, Any]:
        item = self.get_item(doc_id)
        path = str(item.get("analysis_path") or "").strip()
        if not path:
            raise FileNotFoundError(f"analysis artifact not found for doc_id: {doc_id}")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"analysis artifact path does not exist: {path}")
        try:
            with p.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            raise RuntimeError(f"failed to parse analysis artifact: {exc}")
        if not isinstance(payload, dict):
            raise RuntimeError("analysis artifact is not a JSON object")
        return {
            "doc_id": str(doc_id),
            "item": item,
            "analysis_path": path,
            "analysis": payload,
        }

    def _write_analysis_artifact(self, ticker: str, doc_id: str, payload: Dict[str, Any]) -> str:
        t_dir = self._artifact_dir / _norm_ticker(ticker)
        t_dir.mkdir(parents=True, exist_ok=True)
        path = t_dir / f"{doc_id}.analysis.json"
        _safe_json_dump(path, payload)
        return str(path)

    def _pick_next_queued(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._items.values() if str(r.get("status") or "") == "queued"]
        if not rows:
            return None
        rows.sort(key=lambda x: (int(x.get("priority") or 999), str(x.get("discovered_at") or "")))
        return dict(rows[0])

    def _analyze_item(self, doc_id: str, *, use_ai: bool) -> Dict[str, Any]:
        with self._lock:
            item = self._items.get(doc_id)
            if not isinstance(item, dict):
                raise KeyError(f"doc_id not found: {doc_id}")
            # mark in-flight
            item["status"] = "processing"
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = ""
            self._persist_state()

        service = self._get_report_source_service()
        url = str(item.get("url") or "")
        snap = service.fetcher.fetch_page(url)
        if not snap:
            with self._lock:
                item["status"] = "failed"
                item["last_error"] = "fetch_failed"
                item["last_analyzed_at"] = _utc_iso_now()
                self._persist_state()
            raise RuntimeError("failed to fetch document")

        text = str(snap.text or "").strip()
        title = str(snap.title or "").strip()
        if _looks_like_access_challenge(title=title, text=text):
            with self._lock:
                item["status"] = "failed"
                item["last_error"] = "blocked_by_waf"
                item["last_analyzed_at"] = _utc_iso_now()
                item["title"] = title
                item["content_type"] = snap.content_type
                item["final_url"] = snap.final_url
                self._events.append(
                    {
                        "type": "analyze_blocked",
                        "at": item["last_analyzed_at"],
                        "doc_id": doc_id,
                        "ticker": item.get("ticker"),
                        "reason": "blocked_by_waf",
                    }
                )
                self._events = self._events[-500:]
                self._persist_state()
            raise RuntimeError("blocked by WAF/challenge page")

        heuristic = _heuristic_deep_analysis(
            ticker=str(item.get("ticker") or ""),
            doc_item=item,
            title=title,
            text=text,
        )
        ai_result = None
        if use_ai:
            ai_result = self._extractor.extract(
                ticker=str(item.get("ticker") or ""),
                doc_meta={
                    "doc_type": item.get("doc_type"),
                    "url": url,
                    "title": title,
                },
                text=text,
            )

        analysis = {
            "at": _utc_iso_now(),
            "doc_id": doc_id,
            "ticker": item.get("ticker"),
            "source": {
                "url": url,
                "final_url": snap.final_url,
                "status_code": snap.status_code,
                "content_type": snap.content_type,
                "title": title,
            },
            "heuristic": heuristic,
            "ai": ai_result,
            "final": ai_result if isinstance(ai_result, dict) else heuristic,
        }
        artifact_path = self._write_analysis_artifact(str(item.get("ticker") or ""), doc_id, analysis)

        with self._lock:
            item["status"] = "analyzed"
            item["last_analyzed_at"] = _utc_iso_now()
            item["analysis_path"] = artifact_path
            item["analysis_summary"] = str((analysis.get("final") or {}).get("summary") or "")[:500]
            item["title"] = title
            item["content_type"] = snap.content_type
            item["final_url"] = snap.final_url
            self._events.append(
                {
                    "type": "analyze",
                    "at": item["last_analyzed_at"],
                    "doc_id": doc_id,
                    "ticker": item.get("ticker"),
                    "doc_type": item.get("doc_type"),
                    "lane": item.get("lane"),
                    "used_ai": bool(isinstance(ai_result, dict)),
                }
            )
            self._events = self._events[-500:]
            self._persist_state()
            return dict(item)

    def analyze_doc(self, doc_id: str, *, use_ai: bool = True) -> Dict[str, Any]:
        try:
            return self._analyze_item(doc_id, use_ai=use_ai)
        except KeyError as exc:
            raise exc
        except Exception as exc:
            with self._lock:
                item = self._items.get(doc_id)
                if isinstance(item, dict):
                    item["status"] = "failed"
                    item["last_error"] = f"{type(exc).__name__}: {exc}"
                    item["last_analyzed_at"] = _utc_iso_now()
                    self._persist_state()
            raise

    def analyze_next(self, *, use_ai: bool = True) -> Dict[str, Any]:
        nxt = self._pick_next_queued()
        if not isinstance(nxt, dict):
            return {
                "ok": True,
                "message": "no_queued_documents",
                "at": _utc_iso_now(),
            }
        doc_id = str(nxt.get("doc_id") or "")
        if not doc_id:
            return {
                "ok": False,
                "message": "invalid_queue_item",
                "at": _utc_iso_now(),
            }
        item = self.analyze_doc(doc_id, use_ai=use_ai)
        return {
            "ok": True,
            "at": _utc_iso_now(),
            "item": item,
        }


def _get_pipeline() -> ReportSourceDocumentPipeline:
    if _DOC_PIPELINE is None:
        raise HTTPException(status_code=500, detail="report_source document pipeline is not configured")
    return _DOC_PIPELINE


@router.get("/stockflow/report_source/docs/queue/status", summary="查看文档级队列状态")
async def report_source_doc_queue_status() -> Dict[str, Any]:
    return _get_pipeline().status()


@router.get("/stockflow/report_source/docs/queue/items", summary="查看文档级队列项")
async def report_source_doc_queue_items(
    status: str = Query("", description="queued|processing|analyzed|failed"),
    ticker: str = Query("", description="按 ticker 过滤"),
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
) -> Dict[str, Any]:
    return _get_pipeline().list_items(status=status, ticker=ticker, limit=limit)


@router.post("/stockflow/report_source/docs/discover/run_once", summary="执行一次官方链接文档发现并入队")
async def report_source_docs_discover_once(payload: DiscoverDocsRequest) -> Dict[str, Any]:
    pipeline = _get_pipeline()
    return await asyncio.to_thread(
        pipeline.discover_documents,
        payload.tickers,
        force_source_refresh=bool(payload.force_source_refresh),
        max_links_per_source=int(payload.max_links_per_source),
    )


@router.post("/stockflow/report_source/docs/queue/analyze/{doc_id}", summary="分析指定队列文档")
async def report_source_doc_analyze(doc_id: str, payload: AnalyzeDocRequest) -> Dict[str, Any]:
    pipeline = _get_pipeline()
    try:
        item = await asyncio.to_thread(pipeline.analyze_doc, str(doc_id), use_ai=bool(payload.use_ai))
        return {"ok": True, "item": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to analyze {doc_id}: {exc}")


@router.post("/stockflow/report_source/docs/queue/analyze_next", summary="分析下一个待处理文档")
async def report_source_doc_analyze_next(payload: AnalyzeDocRequest) -> Dict[str, Any]:
    pipeline = _get_pipeline()
    try:
        return await asyncio.to_thread(pipeline.analyze_next, use_ai=bool(payload.use_ai))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to analyze next doc: {exc}")


@router.get("/stockflow/report_source/docs/queue/item/{doc_id}", summary="查看单个队列文档项")
async def report_source_doc_queue_item(doc_id: str) -> Dict[str, Any]:
    pipeline = _get_pipeline()
    try:
        item = await asyncio.to_thread(pipeline.get_item, str(doc_id))
        return {"ok": True, "item": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/stockflow/report_source/docs/queue/analysis/{doc_id}", summary="查看已分析文档产物")
async def report_source_doc_queue_analysis(doc_id: str) -> Dict[str, Any]:
    pipeline = _get_pipeline()
    try:
        return await asyncio.to_thread(pipeline.get_analysis, str(doc_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def register_report_source_doc_pipeline_routes(
    app: Any,
    *,
    get_report_source_service: Callable[[], ReportSourceService],
    state_file_path: str,
    artifact_dir: str,
) -> None:
    global _DOC_PIPELINE, _ROUTER_REGISTERED
    _DOC_PIPELINE = ReportSourceDocumentPipeline(
        get_report_source_service=get_report_source_service,
        state_file_path=state_file_path,
        artifact_dir=artifact_dir,
    )
    if _ROUTER_REGISTERED:
        return
    app.include_router(router)
    _ROUTER_REGISTERED = True
