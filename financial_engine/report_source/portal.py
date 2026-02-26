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
    </section>

    <section class="meta">
      <h3>Quick Links</h3>
      <ul class="list">
        <li><a href="/stockflow/report_source/catalog/list?limit=100">/stockflow/report_source/catalog/list</a></li>
        <li><a href="/stockflow/report_source/catalog/item/NVDA">/stockflow/report_source/catalog/item/NVDA</a></li>
        <li><a href="/stockflow/report_source/NVDA?force_refresh=0">/stockflow/report_source/NVDA?force_refresh=0</a></li>
        <li><a href="/earnings/NVDA?force_refresh=0">/earnings/NVDA?force_refresh=0</a></li>
      </ul>
      <div class="footer">建议固定从本页进入，便于团队统一操作路径与验收标准。</div>
    </section>
  </div>

  <script>
    const inputEl = document.getElementById("tickerInput");
    const goBtn = document.getElementById("goBtn");
    const linkEl = document.getElementById("journeyLink");

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
