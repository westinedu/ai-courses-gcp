from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class PageSnapshot:
    url: str
    final_url: str
    status_code: int
    content_type: str
    title: str
    text: str
    links: List[str] = field(default_factory=list)


@dataclass
class CandidateScore:
    url: str
    final_url: str
    source: str
    score: float
    matched_keywords: List[str]
    status_code: int
    title: str
    snippet: str
    company_domain_match: bool
    ai_verified: Optional[bool] = None
    ai_confidence: Optional[float] = None
    ai_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReportSourceResult:
    ticker: str
    company_name: Optional[str]
    company_website: Optional[str]
    ir_home_url: Optional[str]
    financial_reports_url: Optional[str]
    sec_filings_url: Optional[str]
    confidence: float
    verification_status: str
    discovered_at: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def not_found(cls, ticker: str, company_name: Optional[str], company_website: Optional[str], evidence: Dict[str, Any]) -> "ReportSourceResult":
        return cls(
            ticker=ticker.upper(),
            company_name=company_name,
            company_website=company_website,
            ir_home_url=None,
            financial_reports_url=None,
            sec_filings_url=None,
            confidence=0.0,
            verification_status="not_found",
            discovered_at=datetime.utcnow().isoformat() + "Z",
            evidence=evidence,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
