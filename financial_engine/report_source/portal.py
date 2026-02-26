from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from .service import ReportSourceService

router = APIRouter()

_DEFAULT_TICKERS: List[str] = []
_GET_REPORT_SOURCE_SERVICE: Optional[Callable[[], ReportSourceService]] = None
_PORTAL_ROUTER_REGISTERED = False


def _get_service() -> ReportSourceService:
    if _GET_REPORT_SOURCE_SERVICE is None:
        raise HTTPException(status_code=500, detail="report_source service is not configured")
    try:
        return _GET_REPORT_SOURCE_SERVICE()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to initialize report_source service: {exc}")


@router.get("/report_source", summary="Report Source 功能导航页", response_class=HTMLResponse)
async def report_source_home_page() -> HTMLResponse:
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Report Source Portal</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #d9e2ef;
      --blue: #0ea5e9;
      --indigo: #4f46e5;
      --green: #059669;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 10% 10%, rgba(14, 165, 233, .10), transparent 34%),
        radial-gradient(circle at 90% 0%, rgba(79, 70, 229, .10), transparent 36%),
        var(--bg);
    }
    .wrap {
      max-width: 1040px;
      margin: 30px auto 48px;
      padding: 0 16px;
    }
    .hero {
      border-radius: 18px;
      border: 1px solid rgba(148, 163, 184, .35);
      background: linear-gradient(135deg, #0f172a, #112646 56%, #1e1b4b);
      color: #e2e8f0;
      padding: 22px 20px;
      box-shadow: 0 18px 44px rgba(15, 23, 42, .25);
      margin-bottom: 16px;
    }
    .hero h1 {
      margin: 0 0 8px;
      font-size: 30px;
      line-height: 1.15;
      letter-spacing: .01em;
    }
    .hero p {
      margin: 0;
      font-size: 14px;
      color: #cbd5e1;
      line-height: 1.45;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 14px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 10px 28px rgba(15, 23, 42, .05);
    }
    .card h2 {
      margin: 0 0 6px;
      font-size: 17px;
      line-height: 1.25;
    }
    .card p {
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
      border-radius: 10px;
      padding: 9px 13px;
      font-size: 13px;
      font-weight: 700;
      border: 1px solid transparent;
      transition: all .16s ease;
      cursor: pointer;
    }
    .btn-primary {
      color: #fff;
      background: linear-gradient(135deg, var(--blue), var(--indigo));
      box-shadow: 0 8px 22px rgba(79, 70, 229, .26);
    }
    .btn-primary:hover { transform: translateY(-1px); }
    .btn-soft {
      color: #1e293b;
      background: #eef4ff;
      border-color: #c7d8f7;
    }
    .meta {
      background: #ffffff;
      border: 1px dashed #bfd6f6;
      border-radius: 12px;
      padding: 12px;
    }
    .meta h3 {
      margin: 0 0 8px;
      font-size: 14px;
      color: #1e3a8a;
    }
    .list {
      margin: 0;
      padding-left: 18px;
    }
    .list li {
      margin: 6px 0;
      font-size: 13px;
      color: #1e293b;
    }
    .inline {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 8px;
    }
    .inline input {
      border: 1px solid #cbd5e1;
      border-radius: 9px;
      padding: 7px 9px;
      min-width: 130px;
      text-transform: uppercase;
      font-weight: 700;
      letter-spacing: .04em;
    }
    .footer {
      margin-top: 10px;
      color: #64748b;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .hero h1 { font-size: 25px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Report Source Portal</h1>
      <p>财报产品统一入口。你可以在这里管理已验证的 source catalog，或直接进入 earnings journey 开始单股财报分析。</p>
    </section>

    <section class="grid">
      <article class="card">
        <h2>Catalog Workspace</h2>
        <p>批量查看和维护已缓存/已验证的 IR、Financial Reports、SEC 链接，支持刷新和状态追踪。</p>
        <a class="btn btn-primary" href="/report_source/catalog">进入 Catalog</a>
      </article>

      <article class="card">
        <h2>Earnings Journey</h2>
        <p>基于已验证 resource URL + 现有 earnings payload，快速完成一轮“发布后财报拆解”。</p>
        <a id="journeyLink" class="btn btn-primary" href="/report_source/earnings_journey?ticker=NVDA">进入 Journey (NVDA)</a>
        <div class="inline">
          <input id="tickerInput" value="NVDA" maxlength="12" />
          <button id="goBtn" class="btn btn-soft" type="button">按 ticker 打开</button>
        </div>
      </article>

      <article class="card">
        <h2>Pipeline Monitor</h2>
        <p>监控文档发现、排队、分析进度，并直接触发 discover/analyze-next，便于跟踪财报模块运行状态。</p>
        <a id="pipelineLink" class="btn btn-primary" href="/report_source/pipeline_monitor?ticker=NVDA">进入 Monitor (NVDA)</a>
      </article>
    </section>

    <section class="meta">
      <h3>Quick Links</h3>
      <ul class="list">
        <li><a href="/stockflow/report_source/catalog/list?limit=100">/stockflow/report_source/catalog/list</a></li>
        <li><a href="/stockflow/report_source/catalog/item/NVDA">/stockflow/report_source/catalog/item/NVDA</a></li>
        <li><a href="/stockflow/report_source/NVDA?force_refresh=0">/stockflow/report_source/NVDA?force_refresh=0</a></li>
        <li><a href="/earnings/NVDA?force_refresh=0">/earnings/NVDA?force_refresh=0</a></li>
        <li><a href="/stockflow/report_source/docs/queue/status">/stockflow/report_source/docs/queue/status</a></li>
        <li><a href="/stockflow/report_source/monitor/status">/stockflow/report_source/monitor/status</a></li>
      </ul>
      <div class="footer">建议固定从本页进入，便于团队统一操作路径与验收标准。</div>
    </section>
  </div>

  <script>
    const inputEl = document.getElementById("tickerInput");
    const goBtn = document.getElementById("goBtn");
    const linkEl = document.getElementById("journeyLink");
    const pipelineLinkEl = document.getElementById("pipelineLink");

    function normalizeTicker(raw) {
      const t = String(raw || "").toUpperCase().trim();
      if (!/^[A-Z0-9.^=-]{1,12}$/.test(t)) return "";
      return t;
    }

    function openJourney() {
      const ticker = normalizeTicker(inputEl.value);
      if (!ticker) {
        inputEl.focus();
        return;
      }
      linkEl.href = `/report_source/earnings_journey?ticker=${encodeURIComponent(ticker)}`;
      pipelineLinkEl.href = `/report_source/pipeline_monitor?ticker=${encodeURIComponent(ticker)}`;
      window.location.href = linkEl.href;
    }

    goBtn.addEventListener("click", openJourney);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") openJourney();
    });
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)

@router.get("/stockflow/report_source/catalog/list", summary="获取已缓存的财报官网来源目录")
async def list_report_source_catalog(
    limit: int = Query(500, ge=1, le=2000, description="最大返回条数"),
    ticker_prefix: str = Query("", description="按 ticker 前缀过滤"),
) -> Dict[str, Any]:
    service = _get_service()
    return await asyncio.to_thread(service.list_catalog, int(limit), ticker_prefix)


@router.get("/stockflow/report_source/catalog/item/{ticker}", summary="读取单只股票已缓存的财报官网来源（不触发重新抓取）")
async def get_report_source_catalog_item(ticker: str) -> Dict[str, Any]:
    t = str(ticker or "").upper().strip()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required")
    service = _get_service()
    payload = await asyncio.to_thread(service.storage.load, t, 0)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail=f"cached report source not found for {t}")
    payload.setdefault("ticker", t)
    payload["cache"] = {
        "hit": True,
        "mode": "catalog_only",
    }
    return payload


@router.get("/report_source/catalog", summary="财报官网来源目录页面", response_class=HTMLResponse)
async def report_source_catalog_page() -> HTMLResponse:
    default_list = ",".join(_DEFAULT_TICKERS[:20])
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Report Source Catalog</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f5f7fb; color: #0f172a; }}
    .wrap {{ max-width: 1280px; margin: 24px auto; padding: 0 16px; }}
    .panel {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; box-shadow: 0 6px 20px rgba(2, 6, 23, .04); }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    .row {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }}
    input, textarea {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 10px; font-size: 14px; }}
    input {{ width: 140px; }}
    textarea {{ width: 100%; min-height: 58px; }}
    button {{ border: 1px solid #0ea5e9; background: #0ea5e9; color: #fff; border-radius: 8px; padding: 8px 12px; cursor: pointer; font-weight: 600; }}
    button.alt {{ border-color: #94a3b8; background: #fff; color: #334155; }}
    button.warn {{ border-color: #f59e0b; background: #f59e0b; }}
    .meta {{ font-size: 12px; color: #64748b; margin-top: 8px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #eef2f7; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; position: sticky; top: 0; z-index: 2; }}
    .tag {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }}
    .ok {{ background: #dcfce7; color: #166534; }}
    .partial {{ background: #fef9c3; color: #854d0e; }}
    .nf {{ background: #fee2e2; color: #991b1b; }}
    .small {{ font-size: 12px; color: #64748b; }}
    .links a {{ display: block; max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #0369a1; text-decoration: none; }}
    .links a:hover {{ text-decoration: underline; }}
    .sticky {{ position: sticky; top: 0; background: #f5f7fb; padding: 8px 0 12px; z-index: 3; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="sticky">
      <h1>Report Source Catalog</h1>
      <div class="panel">
        <div class="row">
          <input id="limit" type="number" min="1" max="2000" value="500" />
          <input id="prefix" placeholder="Ticker prefix e.g. A" />
          <button id="loadCached">Load Cached Directory</button>
          <button id="loadSample" class="alt">Use Sample 20</button>
          <button id="resolveInput" class="warn">Resolve Input Tickers</button>
          <button id="runUsBatch" class="warn">Run US Tickers (All)</button>
          <button id="resumeUsBatch" class="alt">Resume</button>
          <button id="checkUsBatch" class="alt">Batch Status</button>
          <button id="toggleAutoRefresh" class="alt">Auto Refresh: Off</button>
        </div>
        <textarea id="tickers" placeholder="Comma/space separated tickers, e.g. AAPL, MSFT, NVDA">{default_list}</textarea>
        <div class="meta" id="meta">Ready.</div>
        <div class="meta" id="batchMeta">US batch idle.</div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th style="width:88px;">Ticker</th>
          <th style="width:170px;">Company</th>
          <th style="width:86px;">Status</th>
          <th style="width:86px;">Conf.</th>
          <th>Links</th>
          <th style="width:120px;">Candidates</th>
          <th style="width:170px;">Discovered</th>
          <th style="width:90px;">Action</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <script>
    const meta = document.getElementById("meta");
    const rowsEl = document.getElementById("rows");
    const limitEl = document.getElementById("limit");
    const prefixEl = document.getElementById("prefix");
    const tickersEl = document.getElementById("tickers");
    const resolveInputBtn = document.getElementById("resolveInput");
    const runUsBatchBtn = document.getElementById("runUsBatch");
    const resumeUsBatchBtn = document.getElementById("resumeUsBatch");
    const checkUsBatchBtn = document.getElementById("checkUsBatch");
    const autoRefreshBtn = document.getElementById("toggleAutoRefresh");
    const batchMeta = document.getElementById("batchMeta");
    let currentItems = [];
    let isResolving = false;
    let isUsBatchRunning = false;
    let lastUsBatchState = {{}};
    let autoRefreshTimer = null;
    const autoRefreshMs = 5000;

    function parseTickers(raw) {{
      return Array.from(new Set(
        String(raw || "")
          .toUpperCase()
          .split(/[^A-Z0-9.^=-]+/)
          .map(s => s.trim())
          .filter(Boolean)
      ));
    }}

    function esc(s) {{
      return String(s || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    function statusTag(status) {{
      const s = String(status || "").toLowerCase();
      if (s === "verified") return `<span class="tag ok">verified</span>`;
      if (s === "partial") return `<span class="tag partial">partial</span>`;
      if (s === "error") return `<span class="tag nf">error</span>`;
      return `<span class="tag nf">${{esc(s || "n/a")}}</span>`;
    }}

    function normalizeItem(item) {{
      const out = Object.assign({{}}, item || {{}});
      if (out.candidate_count == null && out.evidence && typeof out.evidence === "object") {{
        out.candidate_count = out.evidence.candidate_count ?? "";
      }}
      return out;
    }}

    function linkBlock(item) {{
      const lines = [];
      if (item.ir_home_url) lines.push(`<a href="${{esc(item.ir_home_url)}}" target="_blank" rel="noopener">IR: ${{esc(item.ir_home_url)}}</a>`);
      if (item.financial_reports_url) lines.push(`<a href="${{esc(item.financial_reports_url)}}" target="_blank" rel="noopener">Reports: ${{esc(item.financial_reports_url)}}</a>`);
      if (item.sec_filings_url) lines.push(`<a href="${{esc(item.sec_filings_url)}}" target="_blank" rel="noopener">SEC: ${{esc(item.sec_filings_url)}}</a>`);
      if (item.error) lines.push(`<span class="small" style="color:#991b1b;">${{esc(item.error)}}</span>`);
      return lines.length ? `<div class="links">${{lines.join("")}}</div>` : `<span class="small">No verified links</span>`;
    }}

    function rowHtml(item) {{
      return `
        <tr>
          <td><strong>${{esc(item.ticker)}}</strong></td>
          <td>${{esc(item.company_name || "")}}</td>
          <td>${{statusTag(item.verification_status)}}</td>
          <td>${{Number(item.confidence || 0).toFixed(3)}}</td>
          <td>${{linkBlock(item)}}</td>
          <td>${{esc(item.candidate_count ?? "")}}</td>
          <td><span class="small">${{esc(item.discovered_at || "")}}</span></td>
          <td><button class="alt" onclick="refreshOne('${{esc(item.ticker)}}')">Refresh</button></td>
        </tr>
      `;
    }}

    function render(items) {{
      currentItems = (items || []).map(normalizeItem);
      rowsEl.innerHTML = currentItems.map(rowHtml).join("");
    }}

    function upsertItem(item, append = true) {{
      const normalized = normalizeItem(item);
      const ticker = String(normalized.ticker || "").toUpperCase();
      if (!ticker) return;
      normalized.ticker = ticker;

      const idx = currentItems.findIndex(x => String((x || {{}}).ticker || "").toUpperCase() === ticker);
      if (idx >= 0) {{
        currentItems[idx] = normalized;
      }} else if (append) {{
        currentItems.push(normalized);
      }} else {{
        currentItems.unshift(normalized);
      }}
      rowsEl.innerHTML = currentItems.map(rowHtml).join("");
    }}

    function setAutoRefreshEnabled(enabled) {{
      if (enabled) {{
        if (autoRefreshTimer) return;
        autoRefreshTimer = setInterval(() => {{
          if (!isResolving) loadCached(true);
          loadUsBatchStatus(true);
        }}, autoRefreshMs);
        autoRefreshBtn.textContent = "Auto Refresh: On";
      }} else {{
        if (autoRefreshTimer) {{
          clearInterval(autoRefreshTimer);
          autoRefreshTimer = null;
        }}
        autoRefreshBtn.textContent = "Auto Refresh: Off";
      }}
    }}

    function renderUsBatchState(state) {{
      const s = (state && typeof state === "object") ? state : {{}};
      lastUsBatchState = s;
      isUsBatchRunning = Boolean(s.running);

      const total = Number(s.total || 0);
      const processed = Number(s.processed || 0);
      const success = Number(s.success || 0);
      const failed = Number(s.failed || 0);
      const startedAt = String(s.started_at || "");
      const finishedAt = String(s.finished_at || "");
      const lastTicker = String(s.last_ticker || "");
      const lastStatus = String(s.last_status || "");
      const lastError = String(s.last_error || "");
      const pct = total > 0 ? ((processed / total) * 100).toFixed(1) : "0.0";

      runUsBatchBtn.disabled = isResolving || isUsBatchRunning;
      resumeUsBatchBtn.disabled = isResolving || isUsBatchRunning;

      if (isUsBatchRunning) {{
        batchMeta.textContent = `US batch running: ${{processed}}/${{total}} (${{pct}}%) | success ${{success}}, failed ${{failed}} | last: ${{lastTicker || "-"}} ${{lastStatus || ""}}`;
      }} else if (total > 0 && startedAt) {{
        batchMeta.textContent = `US batch finished: ${{processed}}/${{total}} | success ${{success}}, failed ${{failed}} | started ${{startedAt}}${{finishedAt ? ` | finished ${{finishedAt}}` : ""}}${{lastError ? ` | last error: ${{lastError}}` : ""}}`;
      }} else {{
        batchMeta.textContent = "US batch idle.";
      }}
    }}

    async function loadUsBatchStatus(silent = false) {{
      try {{
        const res = await fetch("/stockflow/report_source/batch/us_tickers/status");
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(String(data.detail || `HTTP ${{res.status}}`));
        }}
        renderUsBatchState(data);
        if (!silent && data.running) {{
          meta.textContent = `US batch is running: ${{Number(data.processed || 0)}}/${{Number(data.total || 0)}}`;
        }}
      }} catch (err) {{
        if (!silent) {{
          batchMeta.textContent = `US batch status failed: ${{String(err && err.message ? err.message : err)}}`;
        }}
      }}
    }}

    async function runUsBatch() {{
      if (isResolving) {{
        meta.textContent = "Please wait for current resolve to finish.";
        return;
      }}
      if (isUsBatchRunning) {{
        meta.textContent = "US batch already running.";
        await loadUsBatchStatus(false);
        return;
      }}
      if (!window.confirm("Start background resolve for all tickers from us_tickers.json?")) {{
        return;
      }}

      runUsBatchBtn.disabled = true;
      resumeUsBatchBtn.disabled = true;
      try {{
        const res = await fetch("/stockflow/report_source/batch/us_tickers/start?force_refresh=0", {{
          method: "POST",
        }});
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(String(data.detail || `HTTP ${{res.status}}`));
        }}
        renderUsBatchState(data.state || {{}});
        meta.textContent = String(data.message || "US batch started.");
        setAutoRefreshEnabled(true);
        await loadCached(true);
        await loadUsBatchStatus(true);
      }} catch (err) {{
        meta.textContent = `Start US batch failed: ${{String(err && err.message ? err.message : err)}}`;
      }} finally {{
        if (!isUsBatchRunning) {{
          runUsBatchBtn.disabled = isResolving;
          resumeUsBatchBtn.disabled = isResolving;
        }}
      }}
    }}

    async function resumeUsBatch() {{
      if (isResolving) {{
        meta.textContent = "Please wait for current resolve to finish.";
        return;
      }}

      await loadUsBatchStatus(true);
      if (isUsBatchRunning) {{
        meta.textContent = "US batch already running.";
        return;
      }}

      const s = (lastUsBatchState && typeof lastUsBatchState === "object") ? lastUsBatchState : {{}};
      const total = Math.max(0, Number(s.total || 0));
      const processed = Math.max(0, Number(s.processed || 0));
      let startIndex = Number.isFinite(processed) ? Math.floor(processed) : 0;
      const noPersistedProgress = total === 0 && startIndex === 0;
      let resumeHint = null;
      let resumeHintError = "";

      if (noPersistedProgress) {{
        try {{
          const hintRes = await fetch("/stockflow/report_source/batch/us_tickers/resume_hint");
          const hintData = await hintRes.json();
          if (hintRes.ok && hintData && typeof hintData === "object") {{
            resumeHint = hintData;
            const hinted = Math.max(0, Number(hintData.first_uncached_index || 0));
            if (Number.isFinite(hinted)) {{
              startIndex = Math.max(startIndex, Math.floor(hinted));
            }}
          }} else {{
            resumeHintError = String((hintData && hintData.detail) || `HTTP ${{hintRes.status}}`);
          }}
        }} catch (err) {{
          resumeHintError = String(err && err.message ? err.message : err);
        }}
      }}

      if (noPersistedProgress && !resumeHint && resumeHintError) {{
        meta.textContent = `Resume hint unavailable: ${{resumeHintError}}. Falling back to first uncached estimation at start.`;
      }}

      if (total > 0 && startIndex >= total) {{
        meta.textContent = `US batch already complete (${{processed}}/${{total}}). Nothing to resume.`;
        return;
      }}

      if (noPersistedProgress && resumeHint) {{
        const hintTotal = Math.max(0, Number(resumeHint.total || 0));
        if (hintTotal > 0 && startIndex >= hintTotal) {{
          meta.textContent = `All us_tickers appear cached (${{hintTotal}}). Nothing to resume.`;
          return;
        }}
      }}

      const tip = noPersistedProgress
        ? resumeHint
          ? `No persisted batch progress found. Cached detected ${{Number(resumeHint.cached_count || 0)}}/${{Number(resumeHint.total || 0)}}. Resume from index ${{startIndex}}${{resumeHint.first_uncached_ticker ? ` (${{resumeHint.first_uncached_ticker}})` : ""}}?`
          : "No persisted batch progress found. Resume will start from first uncached ticker. Continue?"
        : total > 0
        ? `Resume background resolve from index ${{startIndex}} (${{processed}}/${{total}} processed)?`
        : `Resume background resolve from index ${{startIndex}}?`;
      if (!window.confirm(tip)) {{
        return;
      }}

      runUsBatchBtn.disabled = true;
      resumeUsBatchBtn.disabled = true;
      try {{
        const res = await fetch(`/stockflow/report_source/batch/us_tickers/start?force_refresh=0&resume_from_cached=1&start_index=${{startIndex}}`, {{
          method: "POST",
        }});
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(String(data.detail || `HTTP ${{res.status}}`));
        }}
        renderUsBatchState(data.state || {{}});
        meta.textContent = String(data.message || `US batch resumed from index ${{startIndex}}.`);
        setAutoRefreshEnabled(true);
        await loadCached(true);
        await loadUsBatchStatus(true);
      }} catch (err) {{
        meta.textContent = `Resume US batch failed: ${{String(err && err.message ? err.message : err)}}`;
      }} finally {{
        if (!isUsBatchRunning) {{
          runUsBatchBtn.disabled = isResolving;
          resumeUsBatchBtn.disabled = isResolving;
        }}
      }}
    }}

    async function loadCached(silent = false) {{
      const limit = Math.max(1, Math.min(2000, Number(limitEl.value || 500)));
      const prefix = encodeURIComponent(prefixEl.value || "");
      try {{
        if (!silent) {{
          meta.textContent = "Loading cached directory...";
        }}
        const res = await fetch(`/stockflow/report_source/catalog/list?limit=${{limit}}&ticker_prefix=${{prefix}}`);
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(String(data.detail || `HTTP ${{res.status}}`));
        }}
        const items = Array.isArray(data.items) ? data.items : [];
        render(items);
        const at = new Date().toLocaleTimeString();
        meta.textContent = `Loaded ${{items.length}} records (cached) at ${{at}}${{silent ? " [auto]" : ""}}.`;
      }} catch (err) {{
        meta.textContent = `Load cached failed: ${{String(err && err.message ? err.message : err)}}`;
      }}
    }}

    async function resolveInput() {{
      if (isResolving) {{
        meta.textContent = "Resolve is already running...";
        return;
      }}
      const tickers = parseTickers(tickersEl.value);
      if (!tickers.length) {{
        meta.textContent = "No valid tickers.";
        return;
      }}
      isResolving = true;
      resolveInputBtn.disabled = true;
      runUsBatchBtn.disabled = true;
      resumeUsBatchBtn.disabled = true;
      currentItems = [];
      rowsEl.innerHTML = "";

      let success = 0;
      let failed = 0;
      try {{
        for (let i = 0; i < tickers.length; i++) {{
          const ticker = tickers[i];
          meta.textContent = `Resolving ${{i + 1}}/${{tickers.length}}: ${{ticker}} (success ${{success}}, failed ${{failed}})...`;
          try {{
            const res = await fetch(`/stockflow/report_source/${{encodeURIComponent(ticker)}}?force_refresh=1`);
            const data = await res.json();
            if (!res.ok) {{
              throw new Error(String(data.detail || `HTTP ${{res.status}}`));
            }}
            upsertItem(data, true);
            success += 1;
          }} catch (err) {{
            failed += 1;
            upsertItem({{
              ticker,
              company_name: "",
              verification_status: "error",
              confidence: 0,
              candidate_count: "",
              discovered_at: new Date().toISOString(),
              error: String(err && err.message ? err.message : err),
            }}, true);
          }}
        }}
      }} finally {{
        isResolving = false;
        resolveInputBtn.disabled = false;
        runUsBatchBtn.disabled = isUsBatchRunning;
        resumeUsBatchBtn.disabled = isUsBatchRunning;
      }}
      meta.textContent = `Resolved ${{success}} / ${{tickers.length}} tickers, failed ${{failed}}.`;
    }}

    async function refreshOne(ticker) {{
      if (isResolving) {{
        meta.textContent = "Please wait for current resolve to finish.";
        return;
      }}
      meta.textContent = `Refreshing ${{ticker}}...`;
      try {{
        const res = await fetch(`/stockflow/report_source/${{encodeURIComponent(ticker)}}?force_refresh=1`);
        const item = await res.json();
        if (!res.ok) {{
          throw new Error(String(item.detail || `HTTP ${{res.status}}`));
        }}
        upsertItem(item, false);
        meta.textContent = `Refreshed ${{ticker}}.`;
      }} catch (err) {{
        meta.textContent = `Refresh failed for ${{ticker}}: ${{String(err && err.message ? err.message : err)}}`;
      }}
    }}
    window.refreshOne = refreshOne;

    document.getElementById("loadCached").addEventListener("click", loadCached);
    document.getElementById("resolveInput").addEventListener("click", resolveInput);
    runUsBatchBtn.addEventListener("click", runUsBatch);
    resumeUsBatchBtn.addEventListener("click", resumeUsBatch);
    checkUsBatchBtn.addEventListener("click", () => loadUsBatchStatus(false));
    document.getElementById("loadSample").addEventListener("click", () => {{
      tickersEl.value = "{default_list}";
    }});
    autoRefreshBtn.addEventListener("click", () => {{
      setAutoRefreshEnabled(!autoRefreshTimer);
      if (autoRefreshTimer) {{
        loadCached(true);
        loadUsBatchStatus(true);
      }}
    }});
    window.addEventListener("beforeunload", () => setAutoRefreshEnabled(false));

    loadCached();
    loadUsBatchStatus(true);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/report_source/earnings_journey", summary="财报分析旅程页面（基于已验证 Report Source）", response_class=HTMLResponse)
async def report_source_earnings_journey_page(
    ticker: str = Query("NVDA", description="默认分析股票代码"),
) -> HTMLResponse:
    normalized = str(ticker or "NVDA").strip().upper() or "NVDA"
    default_ticker_json = json.dumps(normalized)
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Earnings Journey</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe3ef;
      --ok-bg: #dcfce7;
      --ok-text: #166534;
      --warn-bg: #fef9c3;
      --warn-text: #854d0e;
      --bad-bg: #fee2e2;
      --bad-text: #991b1b;
      --blue: #0ea5e9;
      --blue-dark: #0369a1;
      --indigo: #4f46e5;
      --green: #059669;
      --red: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 10% 10%, rgba(14, 165, 233, .08), transparent 30%),
        radial-gradient(circle at 80% 0%, rgba(79, 70, 229, .08), transparent 34%),
        var(--bg);
      color: var(--text);
      font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .wrap {
      max-width: 1180px;
      margin: 26px auto 56px;
      padding: 0 16px;
    }
    .hero {
      background: linear-gradient(135deg, #0b1220, #14253e 60%, #0f172a);
      color: #e2e8f0;
      border-radius: 18px;
      padding: 20px 18px;
      border: 1px solid rgba(148, 163, 184, .35);
      box-shadow: 0 18px 40px rgba(15, 23, 42, .22);
      margin-bottom: 16px;
    }
    .hero h1 {
      margin: 0 0 6px;
      font-size: 26px;
      line-height: 1.25;
      letter-spacing: .01em;
    }
    .hero p {
      margin: 0;
      color: #cbd5e1;
      font-size: 14px;
    }
    .toolbar {
      margin-top: 14px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .toolbar input {
      width: 160px;
      border: 1px solid rgba(148, 163, 184, .5);
      border-radius: 10px;
      background: rgba(255, 255, 255, .95);
      color: #0f172a;
      font-size: 14px;
      font-weight: 600;
      text-transform: uppercase;
      padding: 9px 10px;
      letter-spacing: .04em;
    }
    .toolbar button {
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      transition: all .18s ease;
    }
    .btn-primary {
      background: linear-gradient(135deg, #0ea5e9, #4f46e5);
      color: #fff;
      box-shadow: 0 8px 20px rgba(59, 130, 246, .26);
    }
    .btn-primary:hover { transform: translateY(-1px); }
    .btn-soft {
      border-color: rgba(148, 163, 184, .55);
      background: rgba(15, 23, 42, .28);
      color: #dbeafe;
    }
    .meta {
      margin-top: 10px;
      font-size: 12px;
      color: #93c5fd;
      min-height: 1.2em;
      white-space: pre-wrap;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px 14px 12px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, .05);
    }
    .card h2 {
      margin: 0 0 10px;
      font-size: 16px;
      letter-spacing: .01em;
    }
    .small {
      font-size: 12px;
      color: var(--muted);
    }
    .status-tag {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 10px;
      font-size: 12px;
      font-weight: 700;
      margin-left: 8px;
    }
    .status-verified { background: var(--ok-bg); color: var(--ok-text); }
    .status-partial { background: var(--warn-bg); color: var(--warn-text); }
    .status-bad { background: var(--bad-bg); color: var(--bad-text); }
    .source-links {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .source-links a {
      display: block;
      border: 1px solid #dbeafe;
      background: #f8fbff;
      border-radius: 10px;
      text-decoration: none;
      color: var(--blue-dark);
      font-size: 13px;
      line-height: 1.35;
      padding: 8px 10px;
      overflow-wrap: anywhere;
    }
    .source-links a:hover {
      border-color: #93c5fd;
      background: #eff6ff;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 8px;
    }
    .pill {
      border-radius: 999px;
      border: 1px solid #cbd5e1;
      padding: 4px 10px;
      font-size: 12px;
      color: #334155;
      background: #fff;
    }
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      padding: 10px;
      background: #fcfdff;
    }
    .metric .label {
      font-size: 12px;
      color: #64748b;
      margin-bottom: 4px;
    }
    .metric .value {
      font-size: 21px;
      font-weight: 800;
      line-height: 1.2;
      letter-spacing: .01em;
      margin-bottom: 4px;
      color: #0f172a;
    }
    .delta {
      font-size: 12px;
      font-weight: 700;
      display: inline-block;
      margin-right: 10px;
    }
    .up { color: var(--green); }
    .down { color: var(--red); }
    .flat { color: #475569; }
    .section {
      margin-bottom: 14px;
    }
    .list {
      margin: 8px 0 0;
      padding: 0 0 0 18px;
    }
    .list li {
      margin: 6px 0;
      color: #1e293b;
      line-height: 1.45;
      font-size: 14px;
    }
    .factor-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      font-size: 13px;
    }
    .factor-table th, .factor-table td {
      border-bottom: 1px solid #edf2f7;
      padding: 7px 6px;
      text-align: left;
      vertical-align: top;
    }
    .factor-table th {
      font-size: 12px;
      color: #64748b;
      font-weight: 700;
    }
    .loading {
      color: #475569;
      font-size: 13px;
    }
    .error {
      color: #991b1b;
      background: #fff1f2;
      border: 1px solid #fecdd3;
      border-radius: 10px;
      padding: 8px 10px;
      font-size: 13px;
      margin-top: 8px;
      white-space: pre-wrap;
    }
    .journey {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 6px;
    }
    .step {
      border: 1px solid #dbeafe;
      border-radius: 12px;
      background: #f8fbff;
      padding: 10px;
    }
    .step .title {
      font-weight: 700;
      color: #1e3a8a;
      font-size: 13px;
      margin-bottom: 4px;
    }
    .step .desc {
      font-size: 13px;
      color: #1e293b;
      line-height: 1.4;
    }
    @media (max-width: 980px) {
      .grid { grid-template-columns: 1fr; }
      .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .journey { grid-template-columns: 1fr; }
    }
    @media (max-width: 540px) {
      .metrics-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>Earnings Journey</h1>
      <p>从已验证的 Report Source URL 出发，快速拆解财报发布后的核心结论与下一步验证路径。</p>
      <div class="toolbar">
        <input id="tickerInput" placeholder="Ticker, e.g. NVDA" />
        <button class="btn-primary" id="analyzeBtn">开始分析</button>
        <button class="btn-soft" data-ticker="NVDA">NVDA</button>
        <button class="btn-soft" data-ticker="AAPL">AAPL</button>
        <button class="btn-soft" data-ticker="AMD">AMD</button>
        <button class="btn-soft" data-ticker="TSLA">TSLA</button>
      </div>
      <div class="meta" id="meta">Ready.</div>
    </div>

    <div class="grid section">
      <div class="card">
        <h2>Step 1 · Source Grounding <span id="sourceStatus"></span></h2>
        <div id="sourceSummary" class="small loading">等待加载...</div>
        <div id="sourceLinks" class="source-links"></div>
        <div id="sourceError"></div>
      </div>
      <div class="card">
        <h2>Step 2 · Earnings Snapshot</h2>
        <div id="snapshot" class="small loading">等待加载...</div>
        <div id="snapshotError"></div>
      </div>
    </div>

    <div class="card section">
      <h2>Step 3 · Key Metrics Breakdown</h2>
      <div id="metricsGrid" class="metrics-grid">
        <div class="loading">等待加载...</div>
      </div>
      <div id="metricsHint" class="small" style="margin-top:8px;"></div>
    </div>

    <div class="grid section">
      <div class="card">
        <h2>Step 4 · Management Narrative</h2>
        <ul id="interpList" class="list"></ul>
      </div>
      <div class="card">
        <h2>Step 5 · Factor Signal / Confidence</h2>
        <div id="factorSummary" class="small loading">等待加载...</div>
        <table class="factor-table" id="factorTable"></table>
      </div>
    </div>

    <div class="card section">
      <h2>Step 6 · Next Verification Journey</h2>
      <div class="journey" id="journeyChecklist"></div>
    </div>
  </div>

  <script>
    const DEFAULT_TICKER = __DEFAULT_TICKER_JSON__;
    const tickerInput = document.getElementById("tickerInput");
    const analyzeBtn = document.getElementById("analyzeBtn");
    const meta = document.getElementById("meta");
    const sourceStatus = document.getElementById("sourceStatus");
    const sourceSummary = document.getElementById("sourceSummary");
    const sourceLinks = document.getElementById("sourceLinks");
    const sourceError = document.getElementById("sourceError");
    const snapshot = document.getElementById("snapshot");
    const snapshotError = document.getElementById("snapshotError");
    const metricsGrid = document.getElementById("metricsGrid");
    const metricsHint = document.getElementById("metricsHint");
    const interpList = document.getElementById("interpList");
    const factorSummary = document.getElementById("factorSummary");
    const factorTable = document.getElementById("factorTable");
    const journeyChecklist = document.getElementById("journeyChecklist");

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function toNum(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n : null;
    }

    function fmtPct(value) {
      const n = toNum(value);
      if (n == null) return "n/a";
      const v = n * 100;
      return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
    }

    function fmtMoney(value) {
      const n = toNum(value);
      if (n == null) return "n/a";
      const abs = Math.abs(n);
      if (abs >= 1e12) return `${n >= 0 ? "" : "-"}$${(abs / 1e12).toFixed(2)}T`;
      if (abs >= 1e9) return `${n >= 0 ? "" : "-"}$${(abs / 1e9).toFixed(2)}B`;
      if (abs >= 1e6) return `${n >= 0 ? "" : "-"}$${(abs / 1e6).toFixed(2)}M`;
      if (abs >= 1e3) return `${n >= 0 ? "" : "-"}$${(abs / 1e3).toFixed(1)}K`;
      return `${n >= 0 ? "" : "-"}$${abs.toFixed(0)}`;
    }

    function fmtScore(value) {
      const n = toNum(value);
      return n == null ? "n/a" : n.toFixed(3);
    }

    function deltaClass(value) {
      const n = toNum(value);
      if (n == null) return "flat";
      if (n > 0) return "up";
      if (n < 0) return "down";
      return "flat";
    }

    function pickSeries(obj, keys) {
      if (!obj || typeof obj !== "object") return [];
      for (const key of keys) {
        const arr = obj[key];
        if (Array.isArray(arr) && arr.length) return arr.map(toNum);
      }
      return [];
    }

    function safeDate(value) {
      const s = String(value || "").trim();
      if (!s) return "n/a";
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return s;
      return d.toISOString().slice(0, 10);
    }

    function pctChange(series, baseLag) {
      const a = Array.isArray(series) ? series : [];
      if (a.length <= baseLag) return null;
      const current = toNum(a[0]);
      const base = toNum(a[baseLag]);
      if (current == null || base == null || Math.abs(base) < 1e-9) return null;
      return (current - base) / Math.abs(base);
    }

    function calcMargin(numeratorSeries, denominatorSeries) {
      if (!Array.isArray(numeratorSeries) || !Array.isArray(denominatorSeries)) return null;
      const n = toNum(numeratorSeries[0]);
      const d = toNum(denominatorSeries[0]);
      if (n == null || d == null || Math.abs(d) < 1e-9) return null;
      return n / d;
    }

    function normalizeTicker(raw) {
      const t = String(raw || "").toUpperCase().trim();
      if (!t) return "";
      if (!/^[A-Z0-9.^=-]{1,12}$/.test(t)) return "";
      return t;
    }

    async function fetchJson(url) {
      const res = await fetch(url);
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = data && (data.detail || data.error) ? String(data.detail || data.error) : `HTTP ${res.status}`;
        throw new Error(detail);
      }
      return data;
    }

    function renderSource(source, ticker) {
      if (!source || typeof source !== "object") {
        sourceStatus.innerHTML = `<span class="status-tag status-bad">no source</span>`;
        sourceSummary.textContent = `未找到 ${ticker} 的已缓存 report source。`;
        sourceLinks.innerHTML = "";
        return;
      }
      const status = String(source.verification_status || "unknown").toLowerCase();
      let statusClass = "status-bad";
      if (status === "verified") statusClass = "status-verified";
      else if (status === "partial") statusClass = "status-partial";
      sourceStatus.innerHTML = `<span class="status-tag ${statusClass}">${esc(status)}</span>`;

      const candidateCount = source && source.evidence && typeof source.evidence === "object"
        ? Number(source.evidence.candidate_count || 0)
        : 0;
      sourceSummary.innerHTML = `
        <div><strong>${esc(source.company_name || ticker)}</strong> · confidence ${Number(source.confidence || 0).toFixed(3)}</div>
        <div class="row">
          <span class="pill">discovered: ${esc(safeDate(source.discovered_at))}</span>
          <span class="pill">candidates: ${candidateCount || "n/a"}</span>
          <span class="pill">mode: cached verified resource url</span>
        </div>
      `;

      const links = [
        ["IR Home", source.ir_home_url],
        ["Financial Reports", source.financial_reports_url],
        ["SEC Filings", source.sec_filings_url],
      ].filter((item) => !!item[1]);
      sourceLinks.innerHTML = links.length
        ? links.map(([label, url]) => `<a href="${esc(url)}" target="_blank" rel="noopener"><strong>${esc(label)}:</strong> ${esc(url)}</a>`).join("")
        : `<div class="small">No verified links in cached source.</div>`;
    }

    function renderSnapshot(earnings, ticker) {
      if (!earnings || typeof earnings !== "object") {
        snapshot.textContent = `${ticker} 暂无 earnings 载荷。`;
        return;
      }
      const cacheMeta = earnings.cache_meta && typeof earnings.cache_meta === "object" ? earnings.cache_meta : {};
      const stale = Boolean(earnings.stale);
      const staleReason = stale ? ` | staleReason: ${String(earnings.staleReason || "n/a")}` : "";
      snapshot.innerHTML = `
        <div><strong>ticker:</strong> ${esc(String(earnings.ticker || ticker))}</div>
        <div><strong>cacheLayer:</strong> ${esc(String(earnings.cacheLayer || "n/a"))}${stale ? " (stale)" : ""}</div>
        <div><strong>last_updated:</strong> ${esc(String(earnings.last_updated || "n/a"))}</div>
        <div><strong>next_earnings_date:</strong> ${esc(String(cacheMeta.next_earnings_date || "n/a"))}</div>
        <div><strong>refresh_reason:</strong> ${esc(String(cacheMeta.refresh_reason || "n/a"))}${esc(staleReason)}</div>
      `;
    }

    function renderMetrics(earnings) {
      const interp = earnings && typeof earnings === "object" ? (earnings.interpretation_data || {}) : {};
      const earningsData = interp.earnings && typeof interp.earnings === "object" ? interp.earnings : {};
      const financials = interp.financials && typeof interp.financials === "object" ? interp.financials : {};
      const idx = Array.isArray(earningsData.index) ? earningsData.index : (Array.isArray(financials.index) ? financials.index : []);

      const revenue = pickSeries(earningsData, ["Revenue", "Total Revenue"]);
      const netIncome = pickSeries(earningsData, ["Earnings", "Net Income"]);
      const opIncome = pickSeries(financials, ["Operating Income"]);
      const grossProfit = pickSeries(financials, ["Gross Profit"]);
      const fallbackRevenue = revenue.length ? revenue : pickSeries(financials, ["Total Revenue", "Revenue"]);

      const revenueQoq = pctChange(fallbackRevenue, 1);
      const revenueYoy = pctChange(fallbackRevenue, 4);
      const netQoq = pctChange(netIncome, 1);
      const netYoy = pctChange(netIncome, 4);
      const opMargin = calcMargin(opIncome, fallbackRevenue);
      const netMargin = calcMargin(netIncome, fallbackRevenue);
      const grossMargin = calcMargin(grossProfit, fallbackRevenue);

      const cards = [
        {
          label: "Latest Revenue",
          value: fmtMoney(fallbackRevenue[0]),
          qoq: revenueQoq,
          yoy: revenueYoy,
        },
        {
          label: "Latest Net Income",
          value: fmtMoney(netIncome[0]),
          qoq: netQoq,
          yoy: netYoy,
        },
        {
          label: "Operating Margin",
          value: fmtPct(opMargin),
          qoq: null,
          yoy: null,
        },
        {
          label: "Net / Gross Margin",
          value: `${fmtPct(netMargin)} / ${fmtPct(grossMargin)}`,
          qoq: null,
          yoy: null,
        },
      ];

      metricsGrid.innerHTML = cards.map((card) => `
        <div class="metric">
          <div class="label">${esc(card.label)}</div>
          <div class="value">${esc(card.value)}</div>
          <div>
            <span class="delta ${deltaClass(card.qoq)}">QoQ ${fmtPct(card.qoq)}</span>
            <span class="delta ${deltaClass(card.yoy)}">YoY ${fmtPct(card.yoy)}</span>
          </div>
        </div>
      `).join("");

      const latestQuarter = idx && idx.length ? String(idx[0]) : "n/a";
      const prevQuarter = idx && idx.length > 1 ? String(idx[1]) : "n/a";
      metricsHint.textContent = `Quarter basis: latest=${latestQuarter}, prev=${prevQuarter}. YoY 使用 lag=4（若数据不足则显示 n/a）。`;
    }

    function renderInterpretations(earnings) {
      const lines = Array.isArray(earnings && earnings.interpretations) ? earnings.interpretations : [];
      if (!lines.length) {
        interpList.innerHTML = `<li>暂无系统解读，可先从 Source 链接进入 IR/SEC 原文确认关键数字。</li>`;
        return;
      }
      interpList.innerHTML = lines.slice(0, 8).map((line) => `<li>${esc(String(line))}</li>`).join("");
    }

    function renderFactors(earnings) {
      const fs = earnings && typeof earnings === "object" ? (earnings.fundamental_signal || {}) : {};
      const overall = fs && typeof fs === "object" ? (fs.overall || {}) : {};
      const score = toNum(overall.score);
      const conf = toNum(overall.confidence);
      const signal = String(overall.signal || "n/a");
      factorSummary.innerHTML = `
        <div><strong>signal:</strong> ${esc(signal)} | <strong>score:</strong> ${fmtScore(score)} | <strong>confidence:</strong> ${fmtScore(conf)}</div>
        <div class="small">这是财务因子视角，不替代盘中价格行为与指引口径变化。</div>
      `;

      const contrib = fs && typeof fs.factor_contributions === "object" ? fs.factor_contributions : {};
      const rows = Object.entries(contrib)
        .map(([k, v]) => [String(k), toNum(v)])
        .filter((x) => x[1] != null)
        .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
        .slice(0, 8);
      if (!rows.length) {
        factorTable.innerHTML = `<tr><td class="small">No factor contribution rows.</td></tr>`;
        return;
      }
      factorTable.innerHTML = `
        <thead>
          <tr>
            <th>Factor</th>
            <th>Contribution</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${esc(row[0])}</td>
              <td class="${deltaClass(row[1])}">${row[1] >= 0 ? "+" : ""}${row[1].toFixed(4)}</td>
            </tr>
          `).join("")}
        </tbody>
      `;
    }

    function renderJourneyChecklist(source, earnings, ticker) {
      const links = [
        source && source.financial_reports_url ? source.financial_reports_url : "",
        source && source.sec_filings_url ? source.sec_filings_url : "",
        source && source.ir_home_url ? source.ir_home_url : "",
      ].filter(Boolean);
      const nextEarnings = earnings && earnings.cache_meta ? (earnings.cache_meta.next_earnings_date || "n/a") : "n/a";

      const steps = [
        {
          title: "1) 数字确认",
          desc: "先对齐 Revenue / Net Income / Margin 的季度与同比口径，确认一次性项目是否影响可比性。",
        },
        {
          title: "2) 指引拆分",
          desc: "阅读管理层指引，拆分增长来自需求、产品 mix、定价还是供给约束缓解。",
        },
        {
          title: "3) 估值映射",
          desc: "把更新后的增速与利润率映射到估值假设，检查市场隐含预期是否仍过高/过低。",
        },
        {
          title: "4) 风险清单",
          desc: `关注监管、供应链、客户集中度及下一次财报窗口（next earnings: ${nextEarnings}）。`,
        },
      ];

      const linkHtml = links.length
        ? `<div class="small" style="margin-top:6px;">参考链接：${links.map((url) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`).join(" | ")}</div>`
        : `<div class="small" style="margin-top:6px;">${ticker} 暂无可用 verified resource 链接。</div>`;

      journeyChecklist.innerHTML = steps.map((s, i) => `
        <div class="step">
          <div class="title">${esc(s.title)}</div>
          <div class="desc">${esc(s.desc)}</div>
          ${i === 0 ? linkHtml : ""}
        </div>
      `).join("");
    }

    function clearErrors() {
      sourceError.innerHTML = "";
      snapshotError.innerHTML = "";
    }

    async function loadJourney(rawTicker) {
      const ticker = normalizeTicker(rawTicker);
      if (!ticker) {
        meta.textContent = "Ticker 格式无效。示例：NVDA / AAPL / BRK-B";
        return;
      }
      tickerInput.value = ticker;
      clearErrors();
      meta.textContent = `Loading ${ticker} ...`;
      sourceStatus.innerHTML = "";
      sourceSummary.textContent = "加载 source...";
      sourceLinks.innerHTML = "";
      snapshot.textContent = "加载 earnings...";
      metricsGrid.innerHTML = `<div class="loading">加载指标...</div>`;
      interpList.innerHTML = "";
      factorSummary.textContent = "加载因子...";
      factorTable.innerHTML = "";
      journeyChecklist.innerHTML = "";

      const sourceReq = fetchJson(`/stockflow/report_source/catalog/item/${encodeURIComponent(ticker)}`);
      const earningsReq = fetchJson(`/earnings/${encodeURIComponent(ticker)}?force_refresh=0`);
      const [sourceResult, earningsResult] = await Promise.allSettled([sourceReq, earningsReq]);

      const now = new Date();
      const stamp = now.toLocaleString();

      let sourcePayload = null;
      let earningsPayload = null;

      if (sourceResult.status === "fulfilled") {
        sourcePayload = sourceResult.value;
        renderSource(sourcePayload, ticker);
      } else {
        sourceError.innerHTML = `<div class="error">Source load failed: ${esc(String(sourceResult.reason && sourceResult.reason.message ? sourceResult.reason.message : sourceResult.reason))}</div>`;
        renderSource(null, ticker);
      }

      if (earningsResult.status === "fulfilled") {
        earningsPayload = earningsResult.value;
        renderSnapshot(earningsPayload, ticker);
        renderMetrics(earningsPayload);
        renderInterpretations(earningsPayload);
        renderFactors(earningsPayload);
      } else {
        const errMsg = String(earningsResult.reason && earningsResult.reason.message ? earningsResult.reason.message : earningsResult.reason);
        snapshotError.innerHTML = `<div class="error">Earnings load failed: ${esc(errMsg)}</div>`;
        snapshot.textContent = `${ticker} 当前无法返回 earnings 载荷。`;
        metricsGrid.innerHTML = `<div class="error">无法生成指标卡：${esc(errMsg)}</div>`;
        interpList.innerHTML = `<li>暂无解读（earnings 接口不可用）。</li>`;
        factorSummary.textContent = "暂无因子数据。";
        factorTable.innerHTML = "";
      }

      renderJourneyChecklist(sourcePayload, earningsPayload, ticker);

      const sourceOk = sourceResult.status === "fulfilled";
      const earningsOk = earningsResult.status === "fulfilled";
      meta.textContent = `Loaded ${ticker} at ${stamp} | source: ${sourceOk ? "ok" : "failed"} | earnings: ${earningsOk ? "ok" : "failed"}`;
    }

    analyzeBtn.addEventListener("click", () => loadJourney(tickerInput.value));
    tickerInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadJourney(tickerInput.value);
    });
    document.querySelectorAll("[data-ticker]").forEach((btn) => {
      btn.addEventListener("click", () => loadJourney(btn.getAttribute("data-ticker")));
    });

    tickerInput.value = DEFAULT_TICKER;
    loadJourney(DEFAULT_TICKER);
  </script>
</body>
</html>
"""
    html = html.replace("__DEFAULT_TICKER_JSON__", default_ticker_json)
    return HTMLResponse(content=html)


@router.get("/report_source/pipeline_monitor", summary="财报文档分析流程监控页", response_class=HTMLResponse)
async def report_source_pipeline_monitor_page(
    ticker: str = Query("NVDA", description="默认过滤 ticker"),
    status: str = Query("", description="默认过滤状态"),
    limit: int = Query(120, ge=20, le=500, description="默认展示队列条数"),
) -> HTMLResponse:
    normalized_ticker = str(ticker or "NVDA").strip().upper() or "NVDA"
    normalized_status = str(status or "").strip().lower()
    normalized_limit = int(max(20, min(500, limit)))
    default_ticker_json = json.dumps(normalized_ticker)
    default_status_json = json.dumps(normalized_status)
    default_limit_json = json.dumps(normalized_limit)
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Pipeline Monitor</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe3ef;
      --blue: #0ea5e9;
      --indigo: #4f46e5;
      --green: #059669;
      --red: #dc2626;
      --amber: #d97706;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background:
        radial-gradient(circle at 12% 8%, rgba(14, 165, 233, .08), transparent 34%),
        radial-gradient(circle at 90% 0%, rgba(79, 70, 229, .07), transparent 38%),
        var(--bg);
    }
    .wrap {
      max-width: 1260px;
      margin: 24px auto 48px;
      padding: 0 16px;
    }
    .hero {
      border: 1px solid rgba(148, 163, 184, .35);
      border-radius: 18px;
      color: #e2e8f0;
      background: linear-gradient(140deg, #0b1325, #15305a 55%, #312e81);
      box-shadow: 0 16px 36px rgba(15, 23, 42, .24);
      padding: 18px;
      margin-bottom: 14px;
    }
    .hero h1 {
      margin: 0 0 7px;
      font-size: 28px;
      line-height: 1.2;
    }
    .hero p {
      margin: 0;
      color: #cbd5e1;
      font-size: 14px;
      line-height: 1.45;
    }
    .toolbar {
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .toolbar input, .toolbar select {
      border: 1px solid rgba(148, 163, 184, .55);
      border-radius: 10px;
      padding: 8px 10px;
      font-size: 13px;
      min-height: 36px;
      background: rgba(255, 255, 255, .95);
      color: #0f172a;
    }
    .toolbar input[name="ticker"] {
      width: 150px;
      text-transform: uppercase;
      font-weight: 700;
      letter-spacing: .04em;
    }
    .toolbar input[name="limit"] {
      width: 96px;
    }
    .toolbar-tip {
      font-size: 11px;
      color: #dbeafe;
      opacity: .92;
      white-space: nowrap;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      border-radius: 10px;
      border: 1px solid transparent;
      min-height: 36px;
      padding: 8px 11px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      transition: all .15s ease;
      text-decoration: none;
    }
    .btn:hover { transform: translateY(-1px); }
    .btn-primary {
      color: #fff;
      background: linear-gradient(135deg, var(--blue), var(--indigo));
      box-shadow: 0 8px 18px rgba(79, 70, 229, .22);
    }
    .btn-soft {
      color: #e2e8f0;
      border-color: rgba(148, 163, 184, .55);
      background: rgba(15, 23, 42, .28);
    }
    .btn-ghost {
      color: #1e293b;
      background: #eef4ff;
      border-color: #c7d8f7;
    }
    .btn-warn {
      color: #1f2937;
      background: #fef3c7;
      border-color: #fcd34d;
    }
    .meta {
      margin-top: 10px;
      font-size: 12px;
      color: #93c5fd;
      min-height: 1.2em;
      white-space: pre-wrap;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, .05);
    }
    .card h2 {
      margin: 0 0 8px;
      font-size: 16px;
    }
    .kv {
      display: grid;
      grid-template-columns: 170px 1fr;
      gap: 6px 10px;
      font-size: 13px;
      align-items: baseline;
    }
    .k {
      color: #64748b;
      font-weight: 700;
    }
    .v {
      color: #0f172a;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .list {
      margin: 6px 0 0;
      padding: 0 0 0 18px;
      max-height: 220px;
      overflow: auto;
    }
    .list li {
      margin: 6px 0;
      font-size: 12px;
      line-height: 1.4;
      color: #1e293b;
    }
    .chip-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 6px 0 0;
    }
    .chip {
      border-radius: 999px;
      border: 1px solid #cbd5e1;
      background: #fff;
      padding: 3px 9px;
      font-size: 12px;
      color: #334155;
    }
    .section-title {
      margin: 0 0 8px;
      font-size: 15px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      border: 1px solid #dbe3ef;
      border-radius: 12px;
      overflow: hidden;
      background: #fff;
      box-shadow: 0 8px 24px rgba(15, 23, 42, .05);
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid #edf2f7;
      padding: 7px 8px;
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    th {
      background: #f8fafc;
      font-size: 11px;
      color: #475569;
      font-weight: 800;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .status {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 11px;
      font-weight: 700;
    }
    .status-queued { background: #e0f2fe; color: #075985; }
    .status-processing { background: #e0e7ff; color: #3730a3; }
    .status-analyzed { background: #dcfce7; color: #166534; }
    .status-failed { background: #fee2e2; color: #991b1b; }
    .status-unknown { background: #f1f5f9; color: #334155; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 11px;
    }
    .small {
      font-size: 11px;
      color: #64748b;
    }
    .notes-cell {
      min-width: 260px;
      max-width: 360px;
    }
    .note-details {
      margin: 0;
    }
    .note-details summary {
      cursor: pointer;
      list-style: none;
      outline: none;
    }
    .note-details summary::-webkit-details-marker {
      display: none;
    }
    .note-brief {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      white-space: normal;
      font-size: 11px;
      line-height: 1.35;
      color: #64748b;
    }
    .note-full {
      margin-top: 6px;
      max-height: 140px;
      overflow: auto;
      border: 1px solid #dbe3ef;
      border-radius: 8px;
      background: #f8fafc;
      padding: 6px;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 11px;
      line-height: 1.35;
      color: #334155;
    }
    .ok { color: var(--green); }
    .bad { color: var(--red); }
    .warn { color: var(--amber); }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .actions .btn {
      min-height: 32px;
      padding: 6px 10px;
      font-size: 11px;
      color: #fff;
      border-color: transparent;
    }
    .actions .btn.is-disabled,
    .actions .btn[disabled] {
      color: #94a3b8;
      background: #e5e7eb;
      border-color: #cbd5e1;
      box-shadow: none;
      opacity: 1;
      cursor: not-allowed;
      transform: none;
    }
    .btn-act-analyze {
      background: #2563eb;
      border-color: #1d4ed8;
      box-shadow: 0 4px 12px rgba(37, 99, 235, .25);
    }
    .btn-act-analyze:hover { background: #1d4ed8; }
    .btn-act-view {
      background: #059669;
      border-color: #047857;
      box-shadow: 0 4px 12px rgba(5, 150, 105, .25);
    }
    .btn-act-view:hover { background: #047857; }
    .btn-act-source {
      background: #d97706;
      border-color: #b45309;
      box-shadow: 0 4px 12px rgba(217, 119, 6, .24);
    }
    .btn-act-source:hover { background: #b45309; }
    .btn-act-raw {
      background: #0d9488;
      border-color: #0f766e;
      box-shadow: 0 4px 12px rgba(13, 148, 136, .25);
    }
    .btn-act-raw:hover { background: #0f766e; }
    .actions a.btn { text-decoration: none; }
    .home-link {
      margin-left: auto;
    }
    @media (max-width: 1040px) {
      .grid { grid-template-columns: 1fr; }
      .kv { grid-template-columns: 145px 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Pipeline Monitor</h1>
      <p>监控财报文档发现、排队、AI 分析与监听器运行状态。默认按 ticker 聚焦，也可切换为全局视角。</p>
      <div class="toolbar">
        <input name="ticker" id="tickerInput" placeholder="Ticker, e.g. NVDA" />
        <select id="statusSelect">
          <option value="">all status</option>
          <option value="queued">queued</option>
          <option value="processing">processing</option>
          <option value="analyzed">analyzed</option>
          <option value="failed">failed</option>
        </select>
        <input name="limit" id="limitInput" type="number" min="20" max="500" step="10" title="队列列表最大展示条数，范围 20-500。数字越大，刷新会更慢。" />
        <span class="toolbar-tip" id="limitTip">最多显示 120 条队列项（20-500）</span>
        <button class="btn btn-primary" id="refreshBtn" type="button">刷新状态</button>
        <button class="btn btn-ghost" id="discoverBtn" type="button">Discover Run Once</button>
        <button class="btn btn-ghost" id="analyzeNextBtn" type="button">Analyze Next</button>
        <button class="btn btn-warn" id="monitorRunBtn" type="button">Monitor Run Once</button>
        <button class="btn btn-soft" id="autoRefreshBtn" type="button">Auto Refresh: Off</button>
        <a class="btn btn-soft home-link" href="/report_source">返回 Portal</a>
      </div>
      <div class="meta" id="meta">Ready.</div>
    </section>

    <section class="grid">
      <article class="card">
        <h2>Listener Runtime</h2>
        <div class="kv" id="monitorKv"></div>
        <div class="small" style="margin-top:7px;">recent events</div>
        <ul class="list" id="monitorEvents"></ul>
      </article>
      <article class="card">
        <h2>Queue Summary</h2>
        <div class="kv" id="queueKv"></div>
        <div class="small" style="margin-top:7px;">by status / lane / doc type</div>
        <div class="chip-row" id="queueChips"></div>
        <div class="small" style="margin-top:7px;">recent events</div>
        <ul class="list" id="queueEvents"></ul>
      </article>
    </section>

    <h2 class="section-title">Queue Items</h2>
    <table>
      <thead>
        <tr>
          <th style="width:130px;">doc_id</th>
          <th style="width:70px;">ticker</th>
          <th style="width:70px;">type</th>
          <th style="width:92px;">lane</th>
          <th style="width:86px;">status</th>
          <th style="width:62px;">prio</th>
          <th style="width:66px;">attempts</th>
          <th style="width:130px;">discovered</th>
          <th style="width:130px;">last analyzed</th>
          <th style="width:320px;">notes</th>
          <th style="width:340px;">action</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <script>
    const DEFAULT_TICKER = __DEFAULT_TICKER_JSON__;
    const DEFAULT_STATUS = __DEFAULT_STATUS_JSON__;
    const DEFAULT_LIMIT = __DEFAULT_LIMIT_JSON__;
    const AUTO_REFRESH_MS = 10000;

    const tickerInput = document.getElementById("tickerInput");
    const statusSelect = document.getElementById("statusSelect");
    const limitInput = document.getElementById("limitInput");
    const refreshBtn = document.getElementById("refreshBtn");
    const discoverBtn = document.getElementById("discoverBtn");
    const analyzeNextBtn = document.getElementById("analyzeNextBtn");
    const monitorRunBtn = document.getElementById("monitorRunBtn");
    const autoRefreshBtn = document.getElementById("autoRefreshBtn");
    const meta = document.getElementById("meta");
    const rows = document.getElementById("rows");
    const monitorKv = document.getElementById("monitorKv");
    const monitorEvents = document.getElementById("monitorEvents");
    const queueKv = document.getElementById("queueKv");
    const queueEvents = document.getElementById("queueEvents");
    const queueChips = document.getElementById("queueChips");
    const limitTip = document.getElementById("limitTip");

    let autoTimer = null;
    let inFlight = false;

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function normalizeTicker(raw) {
      const t = String(raw || "").trim().toUpperCase();
      if (!t) return "";
      if (!/^[A-Z0-9.^=-]{1,12}$/.test(t)) return "";
      return t;
    }

    function normalizeLimit(raw) {
      const n = Number(raw);
      if (!Number.isFinite(n)) return DEFAULT_LIMIT;
      return Math.max(20, Math.min(500, Math.floor(n)));
    }

    function renderLimitTip() {
      if (!limitTip) return;
      const limit = normalizeLimit(limitInput.value);
      limitTip.textContent = `最多显示 ${limit} 条队列项（20-500）`;
    }

    function fmtTime(value) {
      if (!value) return "n/a";
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return String(value);
      return d.toLocaleString();
    }

    function shortId(docId) {
      const s = String(docId || "");
      if (s.length <= 14) return s;
      return `${s.slice(0, 8)}...${s.slice(-4)}`;
    }

    function boolText(v) {
      return v ? "yes" : "no";
    }

    async function fetchJson(url, options = undefined) {
      const res = await fetch(url, options);
      let data = null;
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
      if (!res.ok) {
        const detail = data && typeof data === "object" ? (data.detail || data.message || JSON.stringify(data)) : `HTTP ${res.status}`;
        throw new Error(String(detail));
      }
      return data;
    }

    function renderMonitor(payload) {
      const cfg = payload && typeof payload.config === "object" ? payload.config : {};
      const rt = payload && typeof payload.runtime === "object" ? payload.runtime : {};
      monitorKv.innerHTML = `
        <div class="k">enabled</div><div class="v ${cfg.enabled ? "ok" : "warn"}">${esc(boolText(Boolean(cfg.enabled)))}</div>
        <div class="k">worker running</div><div class="v ${rt.worker_running ? "ok" : "warn"}">${esc(boolText(Boolean(rt.worker_running)))}</div>
        <div class="k">thread alive</div><div class="v ${rt.thread_alive ? "ok" : "warn"}">${esc(boolText(Boolean(rt.thread_alive)))}</div>
        <div class="k">phase</div><div class="v">${esc(rt.phase || "n/a")}</div>
        <div class="k">next run at</div><div class="v">${esc(fmtTime(rt.next_run_at))}</div>
        <div class="k">last run at</div><div class="v">${esc(fmtTime(rt.last_run_at))}</div>
        <div class="k">last reason</div><div class="v">${esc(rt.last_reason || "n/a")}</div>
        <div class="k">last error</div><div class="v ${rt.last_error ? "bad" : ""}">${esc(rt.last_error || "none")}</div>
        <div class="k">earnings-day tickers</div><div class="v">${esc((Array.isArray(rt.earnings_day_tickers) ? rt.earnings_day_tickers.join(", ") : "") || "n/a")}</div>
        <div class="k">normal mode</div><div class="v">${esc(cfg.normal_mode || "n/a")}</div>
        <div class="k">normal interval</div><div class="v">${esc(String(cfg.normal_interval_minutes || "n/a"))} min</div>
        <div class="k">earnings-day interval</div><div class="v">${esc(String(cfg.earnings_day_interval_minutes || "n/a"))} min</div>
      `;

      const events = Array.isArray(payload && payload.recent_events) ? payload.recent_events : [];
      if (!events.length) {
        monitorEvents.innerHTML = "<li class='small'>No monitor events yet.</li>";
      } else {
        monitorEvents.innerHTML = events.slice(-20).reverse().map((evt) => {
          const at = fmtTime(evt && evt.at);
          const type = String(evt && evt.type ? evt.type : "event");
          const msg = evt && evt.message ? String(evt.message) : "";
          const changes = Number(evt && evt.changes || 0);
          const checked = Number(evt && evt.checked_urls || 0);
          const seeded = Number(evt && evt.newly_seeded || 0);
          return `<li><span class="mono">${esc(at)}</span> | <strong>${esc(type)}</strong> | checked=${esc(String(checked))}, changes=${esc(String(changes))}, seeded=${esc(String(seeded))}${msg ? ` | ${esc(msg)}` : ""}</li>`;
        }).join("");
      }
    }

    function queueMapToChips(title, obj) {
      const src = obj && typeof obj === "object" ? obj : {};
      const pairs = Object.entries(src).sort((a, b) => String(a[0]).localeCompare(String(b[0])));
      if (!pairs.length) return [`<span class="chip">${esc(title)}: n/a</span>`];
      return pairs.map(([k, v]) => `<span class="chip">${esc(title)} ${esc(String(k))}: ${esc(String(v))}</span>`);
    }

    function renderQueue(payload) {
      const byStatus = payload && typeof payload.by_status === "object" ? payload.by_status : {};
      const byType = payload && typeof payload.by_doc_type === "object" ? payload.by_doc_type : {};
      const byLane = payload && typeof payload.by_lane === "object" ? payload.by_lane : {};
      const recentEvents = Array.isArray(payload && payload.recent_events) ? payload.recent_events : [];
      queueKv.innerHTML = `
        <div class="k">total items</div><div class="v">${esc(String(payload && payload.items != null ? payload.items : 0))}</div>
        <div class="k">AI extractor configured</div><div class="v ${payload && payload.ai_extractor_configured ? "ok" : "warn"}">${esc(boolText(Boolean(payload && payload.ai_extractor_configured)))}</div>
        <div class="k">state file</div><div class="v mono">${esc(payload && payload.state_file ? payload.state_file : "n/a")}</div>
        <div class="k">artifact dir</div><div class="v mono">${esc(payload && payload.artifact_dir ? payload.artifact_dir : "n/a")}</div>
        <div class="k">raw dir</div><div class="v mono">${esc(payload && payload.raw_dir ? payload.raw_dir : "n/a")}</div>
      `;
      const chips = [
        ...queueMapToChips("status", byStatus),
        ...queueMapToChips("lane", byLane),
        ...queueMapToChips("type", byType),
      ];
      queueChips.innerHTML = chips.join("");

      if (!recentEvents.length) {
        queueEvents.innerHTML = "<li class='small'>No queue events yet.</li>";
      } else {
        queueEvents.innerHTML = recentEvents.slice(-25).reverse().map((evt) => {
          const at = fmtTime(evt && evt.at);
          const type = String(evt && evt.type ? evt.type : "event");
          const ticker = String(evt && evt.ticker ? evt.ticker : "");
          const docType = String(evt && evt.doc_type ? evt.doc_type : "");
          const lane = String(evt && evt.lane ? evt.lane : "");
          const via = String(evt && evt.fetch_via ? evt.fetch_via : "");
          const discovered = evt && evt.discovered != null ? ` discovered=${evt.discovered}` : "";
          const updated = evt && evt.updated != null ? ` updated=${evt.updated}` : "";
          return `<li><span class="mono">${esc(at)}</span> | <strong>${esc(type)}</strong>${ticker ? ` | ${esc(ticker)}` : ""}${docType ? ` | ${esc(docType)}` : ""}${lane ? ` | ${esc(lane)}` : ""}${via ? ` | via=${esc(via)}` : ""}${discovered}${updated}</li>`;
        }).join("");
      }
    }

    function statusCls(status) {
      const s = String(status || "").toLowerCase();
      if (s === "queued") return "status status-queued";
      if (s === "processing") return "status status-processing";
      if (s === "analyzed") return "status status-analyzed";
      if (s === "failed") return "status status-failed";
      return "status status-unknown";
    }

    function renderItems(payload) {
      const items = Array.isArray(payload && payload.items) ? payload.items : [];
      if (!items.length) {
        rows.innerHTML = "<tr><td colspan='11' class='small'>No queue items for current filters.</td></tr>";
        return;
      }
      rows.innerHTML = items.map((item) => {
        const docId = String(item && item.doc_id ? item.doc_id : "");
        const sourceUrl = String(item && item.url ? item.url : "");
        const hasAnalysis = Boolean(item && item.analysis_path);
        const hasRaw = Boolean(item && item.raw_path);
        const analysisLink = hasAnalysis
          ? `<a class="btn btn-act-view" href="/stockflow/report_source/docs/queue/analysis/${encodeURIComponent(docId)}" target="_blank" rel="noopener">View Analysis</a>`
          : `<button class="btn is-disabled" type="button" disabled title="尚未生成 analysis">View Analysis</button>`;
        const rawLink = hasRaw
          ? `<a class="btn btn-act-raw" href="/report_source/raw_preview?doc_id=${encodeURIComponent(docId)}" target="_blank" rel="noopener">Raw Preview</a>`
          : `<button class="btn is-disabled" type="button" disabled title="尚未落盘 raw 文件">View Raw</button>`;
        const sourceLink = sourceUrl
          ? `<a class="btn btn-act-source" href="${esc(sourceUrl)}" target="_blank" rel="noopener">Open Source</a>`
          : `<button class="btn is-disabled" type="button" disabled>Open Source</button>`;
        const briefParts = [];
        const detailLines = [];
        if (item && item.source_kind) {
          briefParts.push(`source=${item.source_kind}`);
          detailLines.push(`source_kind: ${item.source_kind}`);
        }
        if (item && item.fetch_via) {
          briefParts.push(`via=${item.fetch_via}`);
          detailLines.push(`fetch_via: ${item.fetch_via}`);
        }
        if (item && item.parent_doc_id) {
          briefParts.push(`parent=${String(item.parent_doc_id).slice(0, 8)}`);
          detailLines.push(`parent_doc_id: ${item.parent_doc_id}`);
        }
        if (item && (item.child_discovered != null || item.child_updated != null || item.child_candidates != null)) {
          const discovered = Number(item.child_discovered || 0);
          const updated = Number(item.child_updated || 0);
          const accepted = Number(item.child_candidates || 0);
          briefParts.push(`child d/u=${discovered}/${updated}`);
          detailLines.push(`child_candidates: ${accepted}`);
          detailLines.push(`child_discovered: ${discovered}`);
          detailLines.push(`child_updated: ${updated}`);
          if (item.q4_feed_accepted != null || item.q4_reports_selected != null) {
            const q4Accepted = Number(item.q4_feed_accepted || 0);
            const q4Reports = Number(item.q4_reports_selected || 0);
            briefParts.push(`q4=${q4Accepted}/${q4Reports}`);
            detailLines.push(`q4_feed_accepted: ${q4Accepted}`);
            detailLines.push(`q4_reports_selected: ${q4Reports}`);
          }
        }
        if (item && item.q4_report_title) {
          briefParts.push(`latest=${String(item.q4_report_title).slice(0, 36)}`);
          detailLines.push(`q4_report_title: ${item.q4_report_title}`);
        }
        if (item && item.q4_doc_title) detailLines.push(`q4_doc_title: ${item.q4_doc_title}`);
        if (item && item.q4_doc_category) detailLines.push(`q4_doc_category: ${item.q4_doc_category}`);
        if (item && item.q4_fetch_via) detailLines.push(`q4_fetch_via: ${item.q4_fetch_via}`);
        if (item && item.last_error) {
          briefParts.push(`error=${item.last_error.replace(/^RuntimeError:\\s*/i, "")}`);
          detailLines.push(`last_error: ${item.last_error}`);
        }
        if (item && item.analysis_summary) {
          briefParts.push(`summary=${String(item.analysis_summary).slice(0, 90)}`);
          detailLines.push(`analysis_summary: ${item.analysis_summary}`);
        }
        if (item && item.raw_size_bytes != null && Number(item.raw_size_bytes) > 0) {
          const kb = (Number(item.raw_size_bytes) / 1024).toFixed(1);
          briefParts.push(`raw=${kb}KB`);
          detailLines.push(`raw_size_bytes: ${item.raw_size_bytes}`);
        }
        if (item && item.analysis_path) detailLines.push(`analysis_path: ${item.analysis_path}`);
        if (item && item.raw_path) detailLines.push(`raw_path: ${item.raw_path}`);
        if (item && item.url) detailLines.push(`url: ${item.url}`);

        const briefText = briefParts.join(" | ") || "No extra notes";
        const detailText = detailLines.join("\\n");
        const noteHtml = detailText
          ? `<details class="note-details"><summary class="note-brief" title="${esc(briefText)}">${esc(briefText)}</summary><pre class="note-full">${esc(detailText)}</pre></details>`
          : `<div class="note-brief">${esc(briefText)}</div>`;
        return `
          <tr>
            <td class="mono" title="${esc(docId)}">${esc(shortId(docId))}</td>
            <td><strong>${esc(item && item.ticker ? item.ticker : "")}</strong></td>
            <td>${esc(item && item.doc_type ? item.doc_type : "")}</td>
            <td>${esc(item && item.lane ? item.lane : "")}</td>
            <td><span class="${statusCls(item && item.status)}">${esc(item && item.status ? item.status : "unknown")}</span></td>
            <td>${esc(String(item && item.priority != null ? item.priority : ""))}</td>
            <td>${esc(String(item && item.attempts != null ? item.attempts : 0))}</td>
            <td>${esc(fmtTime(item && item.discovered_at))}</td>
            <td>${esc(fmtTime(item && item.last_analyzed_at))}</td>
            <td class="notes-cell">${noteHtml}</td>
            <td>
              <div class="actions">
                <button class="btn btn-act-analyze" type="button" onclick="analyzeDoc('${esc(docId)}')">Analyze</button>
                ${analysisLink}
                ${rawLink}
                ${sourceLink}
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    function setBusy(flag) {
      inFlight = flag;
      refreshBtn.disabled = flag;
      discoverBtn.disabled = flag;
      analyzeNextBtn.disabled = flag;
      monitorRunBtn.disabled = flag;
    }

    function readFilters() {
      const ticker = normalizeTicker(tickerInput.value);
      const status = String(statusSelect.value || "").trim().toLowerCase();
      const limit = normalizeLimit(limitInput.value);
      tickerInput.value = ticker;
      limitInput.value = String(limit);
      renderLimitTip();
      return { ticker, status, limit };
    }

    function updateUrlParams() {
      const f = readFilters();
      const q = new URLSearchParams(window.location.search);
      if (f.ticker) q.set("ticker", f.ticker); else q.delete("ticker");
      if (f.status) q.set("status", f.status); else q.delete("status");
      q.set("limit", String(f.limit));
      const nextUrl = `${window.location.pathname}?${q.toString()}`;
      window.history.replaceState({}, "", nextUrl);
    }

    async function loadAll(silent = false) {
      if (inFlight) return;
      setBusy(true);
      const startedAt = new Date();
      try {
        updateUrlParams();
        const f = readFilters();
        if (!silent) {
          meta.textContent = `Loading monitor + queue ...`;
        }

        const queueItemsUrl = new URL("/stockflow/report_source/docs/queue/items", window.location.origin);
        queueItemsUrl.searchParams.set("limit", String(f.limit));
        if (f.status) queueItemsUrl.searchParams.set("status", f.status);
        if (f.ticker) queueItemsUrl.searchParams.set("ticker", f.ticker);

        const [monitorRes, queueRes, itemsRes] = await Promise.allSettled([
          fetchJson("/stockflow/report_source/monitor/status"),
          fetchJson("/stockflow/report_source/docs/queue/status"),
          fetchJson(queueItemsUrl.toString()),
        ]);

        let okCount = 0;
        if (monitorRes.status === "fulfilled") {
          renderMonitor(monitorRes.value);
          okCount += 1;
        } else {
          monitorKv.innerHTML = `<div class="k">error</div><div class="v bad">${esc(String(monitorRes.reason && monitorRes.reason.message ? monitorRes.reason.message : monitorRes.reason))}</div>`;
          monitorEvents.innerHTML = "";
        }

        if (queueRes.status === "fulfilled") {
          renderQueue(queueRes.value);
          okCount += 1;
        } else {
          queueKv.innerHTML = `<div class="k">error</div><div class="v bad">${esc(String(queueRes.reason && queueRes.reason.message ? queueRes.reason.message : queueRes.reason))}</div>`;
          queueChips.innerHTML = "";
          queueEvents.innerHTML = "";
        }

        if (itemsRes.status === "fulfilled") {
          renderItems(itemsRes.value);
          okCount += 1;
        } else {
          rows.innerHTML = `<tr><td colspan='11' class='bad'>items load failed: ${esc(String(itemsRes.reason && itemsRes.reason.message ? itemsRes.reason.message : itemsRes.reason))}</td></tr>`;
        }

        const elapsed = Math.max(1, Math.round((Date.now() - startedAt.getTime()) / 10) / 100);
        meta.textContent = `Loaded ${okCount}/3 blocks at ${new Date().toLocaleTimeString()} (${elapsed}s).`;
      } catch (err) {
        meta.textContent = `Load failed: ${String(err && err.message ? err.message : err)}`;
      } finally {
        setBusy(false);
      }
    }

    async function runDiscover() {
      const f = readFilters();
      const tickers = f.ticker ? [f.ticker] : [];
      if (!tickers.length) {
        meta.textContent = "请输入 ticker，再执行 discover run once。";
        tickerInput.focus();
        return;
      }
      setBusy(true);
      try {
        meta.textContent = `Running discover for ${tickers.join(", ")} ...`;
        const payload = {
          tickers,
          force_source_refresh: false,
          max_links_per_source: 120,
        };
        const data = await fetchJson("/stockflow/report_source/docs/discover/run_once", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        const discovered = Number(data && data.discovered || 0);
        const updated = Number(data && data.updated || 0);
        meta.textContent = `Discover finished. discovered=${discovered}, updated=${updated}.`;
      } catch (err) {
        meta.textContent = `Discover failed: ${String(err && err.message ? err.message : err)}`;
      } finally {
        setBusy(false);
        await loadAll(true);
      }
    }

    async function runAnalyzeNext() {
      setBusy(true);
      try {
        meta.textContent = "Analyzing next queued document ...";
        const data = await fetchJson("/stockflow/report_source/docs/queue/analyze_next", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ use_ai: true }),
        });
        if (data && data.message === "no_queued_documents") {
          meta.textContent = "Analyze next done: no queued documents.";
        } else {
          const item = data && data.item ? data.item : {};
          meta.textContent = `Analyze next done: ${String(item.ticker || "-")} ${String(item.doc_type || "-")} ${String(item.status || "")}`;
        }
      } catch (err) {
        meta.textContent = `Analyze next failed: ${String(err && err.message ? err.message : err)}`;
      } finally {
        setBusy(false);
        await loadAll(true);
      }
    }

    async function runMonitorOnce() {
      setBusy(true);
      try {
        meta.textContent = "Running URL monitor once ...";
        const data = await fetchJson("/stockflow/report_source/monitor/run_once", {
          method: "POST",
        });
        const changes = Number(data && data.changes || 0);
        const checked = Number(data && data.checked_urls || 0);
        meta.textContent = `Monitor run once done: checked=${checked}, changes=${changes}.`;
      } catch (err) {
        meta.textContent = `Monitor run once failed: ${String(err && err.message ? err.message : err)}`;
      } finally {
        setBusy(false);
        await loadAll(true);
      }
    }

    async function analyzeDoc(docId) {
      const id = String(docId || "").trim();
      if (!id) return;
      setBusy(true);
      try {
        meta.textContent = `Analyzing doc ${id} ...`;
        const data = await fetchJson(`/stockflow/report_source/docs/queue/analyze/${encodeURIComponent(id)}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ use_ai: true }),
        });
        const item = data && data.item ? data.item : {};
        meta.textContent = `Analyze done: ${String(item.ticker || "-")} ${String(item.doc_type || "-")} ${String(item.status || "")}`;
      } catch (err) {
        meta.textContent = `Analyze doc failed: ${String(err && err.message ? err.message : err)}`;
      } finally {
        setBusy(false);
        await loadAll(true);
      }
    }
    window.analyzeDoc = analyzeDoc;

    function setAutoRefresh(enabled) {
      if (enabled) {
        if (autoTimer) return;
        autoTimer = setInterval(() => {
          loadAll(true);
        }, AUTO_REFRESH_MS);
        autoRefreshBtn.textContent = "Auto Refresh: On";
      } else {
        if (!autoTimer) return;
        clearInterval(autoTimer);
        autoTimer = null;
        autoRefreshBtn.textContent = "Auto Refresh: Off";
      }
    }

    refreshBtn.addEventListener("click", () => loadAll(false));
    discoverBtn.addEventListener("click", runDiscover);
    analyzeNextBtn.addEventListener("click", runAnalyzeNext);
    monitorRunBtn.addEventListener("click", runMonitorOnce);
    autoRefreshBtn.addEventListener("click", () => {
      setAutoRefresh(!autoTimer);
      if (autoTimer) {
        loadAll(true);
      }
    });
    tickerInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadAll(false);
    });
    statusSelect.addEventListener("change", () => loadAll(true));
    limitInput.addEventListener("input", renderLimitTip);
    limitInput.addEventListener("change", () => loadAll(true));
    window.addEventListener("beforeunload", () => setAutoRefresh(false));

    tickerInput.value = DEFAULT_TICKER;
    statusSelect.value = DEFAULT_STATUS;
    limitInput.value = String(DEFAULT_LIMIT);
    renderLimitTip();
    loadAll(false);
  </script>
</body>
</html>
"""
    html = html.replace("__DEFAULT_TICKER_JSON__", default_ticker_json)
    html = html.replace("__DEFAULT_STATUS_JSON__", default_status_json)
    html = html.replace("__DEFAULT_LIMIT_JSON__", default_limit_json)
    return HTMLResponse(content=html)


@router.get("/report_source/raw_preview", summary="原始文档预览页", response_class=HTMLResponse)
async def report_source_raw_preview_page(
    doc_id: str = Query("", description="队列文档 ID"),
) -> HTMLResponse:
    normalized_doc_id = str(doc_id or "").strip()
    default_doc_id_json = json.dumps(normalized_doc_id)
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Raw Preview</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --card: #ffffff;
      --line: #dbe3ef;
      --text: #0f172a;
      --muted: #475569;
      --blue: #2563eb;
      --green: #059669;
      --orange: #d97706;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .wrap {
      max-width: 1040px;
      margin: 24px auto 36px;
      padding: 0 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, .05);
      margin-bottom: 12px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 24px;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    input {
      border: 1px solid #cbd5e1;
      border-radius: 9px;
      padding: 8px 10px;
      min-width: 280px;
      font-size: 13px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 7px 10px;
      border-radius: 9px;
      border: 1px solid transparent;
      color: #fff;
      text-decoration: none;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .btn-primary { background: var(--blue); border-color: #1d4ed8; }
    .btn-secondary { background: var(--green); border-color: #047857; }
    .btn-warn { background: var(--orange); border-color: #b45309; }
    .btn-soft {
      color: #1e293b;
      background: #eef4ff;
      border-color: #c7d8f7;
    }
    .btn[disabled] {
      background: #e5e7eb;
      border-color: #cbd5e1;
      color: #94a3b8;
      cursor: not-allowed;
    }
    .meta {
      margin-top: 10px;
      font-size: 12px;
      color: #64748b;
      min-height: 1.2em;
      white-space: pre-wrap;
    }
    .kv {
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 6px 10px;
      align-items: baseline;
      font-size: 13px;
    }
    .k { color: #64748b; font-weight: 700; }
    .v { color: #0f172a; overflow-wrap: anywhere; word-break: break-word; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }
    pre {
      margin: 0;
      border: 1px solid #dbe3ef;
      border-radius: 10px;
      background: #f8fafc;
      padding: 10px;
      max-height: 68vh;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.35;
      font-size: 12px;
    }
    @media (max-width: 740px) {
      .kv { grid-template-columns: 1fr; }
      input { min-width: 220px; width: 100%; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Raw Preview</h1>
      <div class="toolbar">
        <input id="docIdInput" placeholder="doc_id" />
        <button class="btn btn-primary" id="loadBtn" type="button">Load</button>
        <a class="btn btn-soft" id="openMonitorBtn" href="/report_source/pipeline_monitor" target="_blank" rel="noopener">Open Monitor</a>
        <a class="btn btn-secondary" id="analysisBtn" href="#" target="_blank" rel="noopener">View Analysis</a>
        <a class="btn btn-warn" id="sourceBtn" href="#" target="_blank" rel="noopener">Open Source</a>
        <a class="btn btn-primary" id="downloadBtn" href="#" target="_blank" rel="noopener">Download Raw</a>
      </div>
      <div class="meta" id="meta">Ready.</div>
    </div>

    <div class="card">
      <div class="kv" id="metaKv"></div>
    </div>

    <div class="card">
      <pre id="preview">(empty)</pre>
    </div>
  </div>

  <script>
    const DEFAULT_DOC_ID = __DEFAULT_DOC_ID_JSON__;
    const docIdInput = document.getElementById("docIdInput");
    const loadBtn = document.getElementById("loadBtn");
    const meta = document.getElementById("meta");
    const metaKv = document.getElementById("metaKv");
    const preview = document.getElementById("preview");
    const openMonitorBtn = document.getElementById("openMonitorBtn");
    const analysisBtn = document.getElementById("analysisBtn");
    const sourceBtn = document.getElementById("sourceBtn");
    const downloadBtn = document.getElementById("downloadBtn");

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function normalizeDocId(raw) {
      return String(raw || "").trim();
    }

    async function fetchJson(url) {
      const res = await fetch(url);
      let data = null;
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
      if (!res.ok) {
        const detail = data && typeof data === "object" ? (data.detail || data.message || JSON.stringify(data)) : `HTTP ${res.status}`;
        throw new Error(String(detail));
      }
      return data;
    }

    function setActionLinks(docId, item) {
      const safeDocId = encodeURIComponent(docId);
      openMonitorBtn.href = `/report_source/pipeline_monitor?ticker=${encodeURIComponent(String(item && item.ticker ? item.ticker : ""))}`;
      analysisBtn.href = `/stockflow/report_source/docs/queue/analysis/${safeDocId}`;
      downloadBtn.href = `/stockflow/report_source/docs/queue/raw/${safeDocId}?download=1`;
      sourceBtn.href = String(item && item.url ? item.url : "#");
      analysisBtn.style.pointerEvents = item && item.analysis_path ? "auto" : "none";
      analysisBtn.style.opacity = item && item.analysis_path ? "1" : ".45";
      sourceBtn.style.pointerEvents = item && item.url ? "auto" : "none";
      sourceBtn.style.opacity = item && item.url ? "1" : ".45";
    }

    function renderPayload(payload, docId) {
      const item = payload && typeof payload.item === "object" ? payload.item : {};
      const kb = Number(payload && payload.raw_size_bytes || 0) / 1024;
      metaKv.innerHTML = `
        <div class="k">doc_id</div><div class="v mono">${esc(docId)}</div>
        <div class="k">ticker / type</div><div class="v">${esc(String(item.ticker || ""))} / ${esc(String(item.doc_type || ""))}</div>
        <div class="k">status / attempts</div><div class="v">${esc(String(item.status || ""))} / ${esc(String(item.attempts ?? ""))}</div>
        <div class="k">raw path</div><div class="v mono">${esc(String(payload.raw_path || ""))}</div>
        <div class="k">content type</div><div class="v">${esc(String(payload.raw_content_type || ""))}</div>
        <div class="k">size / sha256</div><div class="v">${esc(`${kb.toFixed(1)} KB`)} / <span class="mono">${esc(String(payload.raw_sha256 || ""))}</span></div>
        <div class="k">last error</div><div class="v">${esc(String(item.last_error || "none"))}</div>
      `;
      const text = String(payload && payload.preview_text ? payload.preview_text : "").trim();
      preview.textContent = text || "(binary/no text preview)";
      setActionLinks(docId, item);
    }

    async function loadRaw(rawDocId) {
      const docId = normalizeDocId(rawDocId);
      if (!docId) {
        meta.textContent = "doc_id is required.";
        return;
      }
      docIdInput.value = docId;
      const q = new URLSearchParams(window.location.search);
      q.set("doc_id", docId);
      window.history.replaceState({}, "", `${window.location.pathname}?${q.toString()}`);
      meta.textContent = `Loading ${docId} ...`;
      preview.textContent = "(loading)";
      try {
        const payload = await fetchJson(`/stockflow/report_source/docs/queue/raw/${encodeURIComponent(docId)}`);
        renderPayload(payload, docId);
        meta.textContent = `Loaded at ${new Date().toLocaleString()}`;
      } catch (err) {
        meta.textContent = `Load failed: ${String(err && err.message ? err.message : err)}`;
        metaKv.innerHTML = "";
        preview.textContent = "(failed)";
      }
    }

    loadBtn.addEventListener("click", () => loadRaw(docIdInput.value));
    docIdInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadRaw(docIdInput.value);
    });

    docIdInput.value = DEFAULT_DOC_ID;
    if (DEFAULT_DOC_ID) {
      loadRaw(DEFAULT_DOC_ID);
    }
  </script>
</body>
</html>
"""
    html = html.replace("__DEFAULT_DOC_ID_JSON__", default_doc_id_json)
    return HTMLResponse(content=html)


def register_report_source_portal_routes(
    app: Any,
    *,
    default_tickers: List[str],
    get_report_source_service: Callable[[], ReportSourceService],
) -> None:
    global _DEFAULT_TICKERS, _GET_REPORT_SOURCE_SERVICE, _PORTAL_ROUTER_REGISTERED
    _DEFAULT_TICKERS = [str(t).upper().strip() for t in (default_tickers or []) if str(t).strip()]
    _GET_REPORT_SOURCE_SERVICE = get_report_source_service
    if _PORTAL_ROUTER_REGISTERED:
        return
    app.include_router(router)
    _PORTAL_ROUTER_REGISTERED = True
