"""
api/routes.py — all HTTP handlers, deliberately thin.

Route → one call into analysis/, discussions_svc/, or db. No business logic
here: if a handler grows an `if` about *finance*, it's in the wrong file.
"""
from __future__ import annotations

import datetime as dt
import json

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
from analysis import composer
from cache.store import invalidate
from db import connect
from discussions_svc.parser import list_discussions
from discussions_svc.stream import broadcaster
from providers import registry, schwab
from providers.base import ProviderError

router = APIRouter(prefix="/api")


# ─── analysis ──────────────────────────────────────────────────────────────────


def _home_path(path: str) -> str:
    """Display form of an instance path: '~/...' when under the user's home.
    Portable across machines and — unlike a cwd-relative path — unambiguous
    no matter which folder the CLI session runs in (a cwd-relative path once
    sent a discussion into a repo checkout instead of the instance dir)."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


@router.get("/analysis/{symbol}")
def get_analysis(symbol: str, refresh: bool = Query(False)):
    """The big one — every section for one ticker. `?refresh=true` = the
    universal refresh button (forces past TTLs; still stale-falls-back)."""
    try:
        return composer.analyze(symbol, force=refresh)
    except ProviderError as e:
        if e.reason == "not_found":
            raise HTTPException(404, e.detail or f"No data for '{symbol.upper()}'")
        if e.reason == "auth_expired":
            raise HTTPException(503, f"Schwab auth: {e.detail} (run schwab-reauth)")
        raise HTTPException(502, f"{e.reason}: {e.detail}")


@router.get("/analysis/{symbol}/report")
def get_report(symbol: str):
    """Print-designed research snapshot: full analysis + this ticker's Claude
    discussions + recorded decisions, as self-contained HTML that auto-opens
    the print dialog (⌘P → Save as PDF)."""
    from fastapi.responses import HTMLResponse
    from analysis.screener import decisions_review
    from api.report import build_report_html

    try:
        analysis = composer.analyze(symbol)
    except ProviderError as e:
        if e.reason == "not_found":
            raise HTTPException(404, e.detail or f"No data for '{symbol.upper()}'")
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    discussions = list_discussions(symbol, limit=25)
    decisions = [d for d in decisions_review(_pv()["quote"])
                 if d["ticker"] == symbol.upper()]
    return HTMLResponse(build_report_html(analysis, discussions, decisions))


@router.get("/analysis/{symbol}/prompt")
def get_prompt(symbol: str):
    """The copy-into-Claude-CLI prompt (AskClaudeModal). Server-built so the
    context_hash matches the exact snapshot served (DESIGN_QUESTIONS: backend-
    computed hash) and Q15's position line stays a one-flag decision."""
    analysis = composer.analyze(symbol)
    return {"prompt": build_claude_prompt(analysis),
            "context_hash": analysis["context_hash"]}


def _value_lens_lines(a: dict) -> list[str]:
    """Statement value lens → prompt. These analyses MUST inform the stance:
    the research layer is where fundamentals affect buy/sell in this app —
    computed signals never do (experiment verdict, n≈9,600)."""
    vl = a.get("value_lens")
    if not vl:
        return []
    def v(key, suffix=""):
        m = vl.get(key) or {}
        return f"{m['value']}{suffix}" if m.get("value") is not None else "n/a"
    lines = [
        "",
        "## Statement value lens (weigh these in your stance — REQUIRED)",
        f"- FCF (TTM): ${v('fcf_ttm')}M · P/FCF {v('p_fcf', 'x')} "
        f"(vs {v('p_fcf_avg', 'x')} on multi-FY-avg FCF) · P/E on multi-FY-avg "
        f"earnings {v('pe_avg', 'x')}",
        f"- Dividends paid ÷ FCF: {v('payout_of_fcf_pct', '%')} · EV: ${v('ev')}M"
        + (" · NET CASH position" if vl.get("net_cash") else ""),
        f"- Revenue CAGR: {v('rev_cagr_3y', '%')} (3y) / {v('rev_cagr_5y', '%')} "
        f"(~5y) · ROIC~: {v('roic_ttm', '%')} TTM / {v('roic_avg', '%')} multi-yr "
        "(approximate) · acquisitions "
        f"${v('acquisitions_total')}M over the reported FYs",
    ]
    for r in vl.get("readings") or []:
        lines.append(f"- READ: {r}")
    for w in vl.get("warnings") or []:
        lines.append(f"- FLAG: {w}")
    lines.append(
        "- Your `stance:` must explicitly weigh this lens — cite the metrics "
        "that drive your call AND the ones that argue against it. If the lens "
        "contradicts the price action, say which you trust and why.")
    return lines


def _stale_warnings(a: dict) -> list[str]:
    """If core sections are being served stale (provider failing → cached data),
    the prompt must SAY SO — a research session once anchored its narrative on
    a 4-day-old price because the snapshot looked fresh."""
    warns = []
    for sec in ("quote", "history"):
        env = (a.get("sections") or {}).get(sec) or {}
        if env.get("status") in ("stale", "unavailable"):
            age_h = round((env.get("age_seconds") or 0) / 3600, 1)
            reason = env.get("error_reason", "?")
            warns.append(
                f"- ⚠ STALE DATA: the {sec} below is {age_h}h old (provider "
                f"error: {reason}). Do NOT trust these prices as current — "
                "verify the live price independently before reasoning about "
                "today's move, and say in your analysis that the snapshot was stale.")
    return warns


def build_claude_prompt(a: dict) -> str:
    """Assemble the markdown prompt: analysis context + prior-discussion
    continuity + a strict writing contract + exact frontmatter (the watcher's
    parser depends on it).

    The prompt itself is STATIC per snapshot and always regenerable from the
    button — continuity across Claude sessions comes from the embedded prior
    discussion summaries, not from chat history.
    """
    sym = a["symbol"]
    now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    q, band, move, score = a.get("quote") or {}, a.get("band"), a.get("move"), a.get("setup_score")

    lines = [
        f"# Research discussion: {sym}",
        "",
        f"You are discussing a stock with {config.USER_NAME} for research/education. "
        "IMPORTANT: do NOT predict price direction — the empirical evidence says "
        "these signals cannot (1-day direction ≈ coin flip over ~9,600 backtested "
        "predictions). Ranges and risk framing are fine; directional calls are not.",
        "",
        "## Current analysis snapshot (context for YOU — do not repeat it back)",
        *_stale_warnings(a),
        f"- Price: ${q.get('last')} ({q.get('net_change_pct')}% today), prev close ${q.get('close_prev')}",
    ]
    if band:
        lines.append(f"- 80% expected range for next session: ${band['low']} – ${band['high']} "
                     f"(±{band['half_width_pct']}%; construction covered 80.5% of 9,620 backtest days)")
    if move:
        lines.append(f"- Today's move: {move['move_pct']}% = {move['z']}σ "
                     f"({'outside' if move['outside_band'] else 'inside'} the 80% band)")
    if score:
        comp = ", ".join(f"{c['component']}={c['value']}" for c in score["components"] if c["available"])
        lines.append(f"- Setup Score: {score['score']}/100 ({score['lean']}) from: {comp}")
    for key, label in [("fundamentals", "Fundamentals"), ("news_sentiment", "News sentiment"),
                       ("options", "Options"), ("social", "Social"),
                       ("recommendations", "Analyst recs"), ("earnings", "Earnings"),
                       ("position", "My position")]:
        data = a.get(key)
        if data and (data.get("available", True) or data.get("owned")):
            slim = {k: v for k, v in data.items()
                    if k not in {"available", "sample", "surprises", "months"} and v is not None}
            lines.append(f"- {label}: {json.dumps(slim, default=str)}")

    # ── continuity: what past sessions already concluded about this ticker ────
    prior = list_discussions(sym, limit=5)
    if prior:
        lines += ["", "## Prior discussions on this ticker (build on these — "
                      "don't re-derive settled points; call out explicitly if "
                      "new data changes an earlier conclusion)"]
        for d in prior:
            tags = f" [{', '.join(d['tags'])}]" if d.get("tags") else ""
            lines.append(f"- {d['created_ts']}: {d['summary'] or '(no summary)'}{tags}")

    lines += [
        "",
        "## How to write the analysis — this is the part the user reads",
        "- Audience: an intelligent person with NO finance background. Every "
        "piece of jargon gets a plain-English gloss in the same sentence.",
        "- Detailed reasoning, not conclusions-by-decree: every claim ties to a "
        "number from the snapshot above, and you say WHY it matters.",
        "- If you have web search available, do a quick check of TODAY'S news "
        f"for {sym} and fold anything material into the analysis; then set the "
        "optional `news_view`/`news_note` frontmatter below — the app surfaces "
        "them in its News section.",
        "- If the user states a decision during the conversation (buy / sell / "
        "trim / hold / pass), record it in the optional `decision:` frontmatter "
        "— the app captures the price at that moment automatically and tracks "
        "how the call aged in its Decisions view. Only record THEIR stated "
        "decision; never invent one.",
        "- Separately, give YOUR OWN research stance in the `stance:` "
        "frontmatter (buy|sell|hold|trim|watch) with `stance_horizon:` (e.g. "
        "3-6mo) and a one-line `stance_note:` justification. This is a "
        "REASONED multi-month judgment from the full picture — valuation, "
        "thesis, risk, news — NOT a short-term direction call (those are "
        "empirically dead). If the picture is genuinely unclear, use "
        "`stance: watch` or omit it — an honest 'no stance' beats a forced one. "
        "The app shows your latest stance beside the ticker until a newer "
        "discussion replaces it.",
        "- Structure the body exactly as: `## TL;DR` (2–3 sentences) → "
        "`## What's going on` → `## What the data says` (the reasoning core) → "
        "`## Risks & what would change this view` → `## Bottom line`.",
        "- Do NOT paste this prompt, the snapshot block, or raw JSON into the "
        "file — the app already displays all of that. The file is your "
        "analysis only.",
        "",
        *_value_lens_lines(a),
        "## After the discussion — REQUIRED",
        f"Write your analysis to `{_home_path(os.path.join(config.DISCUSSIONS_DIR, sym, f'{now}.md'))}` "
        "(this path is under the TickerLens home — correct from ANY working "
        "folder; do NOT write into a repo checkout) "
        "with EXACTLY this frontmatter structure (the app auto-ingests it).",
        "ALWAYS create a NEW file per discussion — never append to or edit a "
        "previous discussion file; each conversation is its own timeline entry.",
        "",
        "```markdown",
        "---",
        f"ticker: {sym}",
        f"created_ts: {now}",
        "summary: <your TL;DR compressed to one line — this is the timeline display>",
        "tags: [<one-or-two-kebab-case-tags>]",
        "news_view: <bullish|neutral|bearish — only if you checked the news>",
        "news_note: <one line on what the latest news says — optional>",
        "decision: <buy|sell|trim|hold|pass — ONLY if the user stated one>",
        "stance: <buy|sell|hold|trim|watch — YOUR reasoned multi-month call, or omit>",
        "stance_horizon: <e.g. 3-6mo>",
        "stance_note: <one line of reasoning behind the stance>",
        f"context_hash: {a['context_hash']}",
        "---",
        "## TL;DR",
        "<the analysis body per the structure above>",
        "```",
    ]
    return "\n".join(lines)


def _pv() -> dict:
    """Provider fn map for screener/income/decisions via the registry."""
    r = registry.resolve
    return {"quote": r("quotes"), "history": r("daily_history"),
            "options": r("option_chain"), "earnings": r("earnings"),
            "fundamentals": r("fundamentals")}


@router.get("/watchlist/quotes")
def watchlist_quotes():
    """Fast pass for the screener: ONE batched Schwab call for every watched
    symbol → price + day change. Each quote is written into the shared cache,
    so the subsequent per-symbol enrichment (and any analysis page) reuses
    them instead of re-fetching."""
    from cache.store import put
    conn = connect()
    try:
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist")]
    finally:
        conn.close()
    if not symbols:
        return {}
    fn = registry.resolve("quotes_batch")
    if fn is None:
        return {}
    try:
        quotes = fn(symbols)
    except ProviderError as e:
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    for sym, q in quotes.items():
        put(f"quote:{sym}", q)   # warm the cache — one call feeds everything
    return {sym: {"price": q["last"], "day_pct": q["net_change_pct"],
                  "ah_price": q.get("ah_price"),
                  "ah_change_pct": q.get("ah_change_pct"),
                  "is_extended": bool(q.get("is_extended"))}
            for sym, q in quotes.items()}


@router.get("/watchlist/screener")
def watchlist_screener(refresh: bool = Query(False),
                       symbols: str | None = Query(None)):
    """P1+P4 enrichment: band width vs implied move, days to earnings,
    yesterday's σ (band-break flag), last persisted score.

    `?symbols=A,B,C` limits to a chunk — the UI streams enrichment in small
    batches with a progress bar instead of blocking on the whole list."""
    from analysis.screener import screen_watchlist
    subset = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
              if symbols else None)
    return screen_watchlist(_pv(), force=refresh, only=subset)


@router.get("/portfolio/income")
def portfolio_income_view(refresh: bool = Query(False)):
    """P2: real dividend yields per holding → projected annual income."""
    from analysis.screener import portfolio_income
    try:
        account = composer.portfolio(force=False)["data"]
    except ProviderError as e:
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    return portfolio_income(account, _pv()["fundamentals"], force=refresh)


@router.get("/stances/scorecard")
def stances_scorecard():
    """Forward outcome audit of every recorded research stance vs SPY.
    The honest answer to "is the algorithm accurate": there is no algorithm —
    stances are dated judgments, and this tracks how each aged."""
    from analysis.scorecard import score_stances
    from cache.store import get_or_fetch
    from db import connect

    conn = connect()
    try:
        stances = [dict(r) for r in conn.execute(
            "SELECT ticker, stance, stance_horizon, stance_note, created_ts "
            "FROM claude_discussions WHERE stance IS NOT NULL "
            "ORDER BY created_ts")]
    finally:
        conn.close()
    if not stances:
        return {"rows": [], "summary": {}, "n_scored": 0,
                "caveat": "No stances recorded yet — run research sessions."}

    hist = _pv()["history"]
    bars_by_symbol: dict = {}
    for sym in sorted({s["ticker"].upper() for s in stances}):
        try:
            bars_by_symbol[sym] = get_or_fetch(
                f"history:{sym}", config.TTL["history"],
                lambda sym=sym: hist(sym))["data"]
        except ProviderError:
            bars_by_symbol[sym] = []
    try:
        spy = get_or_fetch("history:SPY", config.TTL["history"],
                           lambda: hist("SPY"))["data"]
    except ProviderError:
        spy = []
    return score_stances(stances, bars_by_symbol, spy)


@router.get("/decisions")
def decisions():
    """P3: every recorded decision + price then vs now."""
    from analysis.screener import decisions_review
    return decisions_review(_pv()["quote"])


# ─── P5: action plan (ported from the old Portfolio app) ───────────────────────

class ActionAdd(BaseModel):
    symbol: str | None = None
    action: str = Field(min_length=1, max_length=200)
    rationale: str = ""
    priority: int = 0


class ActionPatch(BaseModel):
    status: str | None = None      # open / done / dismissed
    rationale: str | None = None


@router.get("/actions")
def list_actions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY (status != 'open'), priority DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/actions", status_code=201)
def add_action(body: ActionAdd):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO actions (symbol, action, rationale, priority, created_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ((body.symbol or "").upper() or None, body.action, body.rationale,
             body.priority, dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()


@router.patch("/actions/{action_id}")
def patch_action(action_id: int, body: ActionPatch):
    if body.status is not None and body.status not in {"open", "done", "dismissed"}:
        raise HTTPException(422, "status must be open|done|dismissed")
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise HTTPException(404, "action not found")
        if body.status is not None:
            conn.execute("UPDATE actions SET status = ? WHERE id = ?", (body.status, action_id))
        if body.rationale is not None:
            conn.execute("UPDATE actions SET rationale = ? WHERE id = ?", (body.rationale, action_id))
        conn.commit()
        return dict(conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone())
    finally:
        conn.close()


@router.post("/actions/import-portfolio")
def import_actions():
    """One-time port of the old app's action plan (read-only source)."""
    import sqlite3 as _sq
    from config import PORTFOLIO_DB_PATH
    import os as _os
    if not PORTFOLIO_DB_PATH:
        raise HTTPException(501, "no legacy portfolio DB configured (paths.portfolio_db)")
    if not _os.path.exists(PORTFOLIO_DB_PATH):
        raise HTTPException(503, "old Portfolio app DB not found")
    try:
        src = _sq.connect(f"file:{PORTFOLIO_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        src.row_factory = _sq.Row
        rows = src.execute("SELECT symbol, action, rationale, status, priority, "
                           "created_ts FROM actions").fetchall()
        src.close()
    except _sq.Error as e:
        raise HTTPException(503, f"old app DB unreadable: {e}")
    conn = connect()
    imported = 0
    try:
        for r in rows:
            dup = conn.execute(
                "SELECT 1 FROM actions WHERE symbol IS ? AND action = ? AND created_ts = ?",
                (r["symbol"], r["action"], r["created_ts"])).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO actions (symbol, action, rationale, status, priority, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (r["symbol"], r["action"], r["rationale"] or "", r["status"] or "open",
                 r["priority"] or 0, r["created_ts"] or dt.datetime.now().isoformat(timespec="seconds")))
            imported += 1
        conn.commit()
        return {"ok": True, "imported": imported, "seen": len(rows)}
    finally:
        conn.close()


# ─── watchlist imports (Schwab attempt + bulk paste) ───────────────────────────

class BulkAdd(BaseModel):
    symbols: str = Field(min_length=1)   # free text: "AAPL, NVDA TSLA\nHIMS"


def _add_watch_symbols(symbols: list[str], tag: str) -> int:
    conn = connect()
    added = 0
    try:
        for sym in symbols:
            sym = sym.upper().strip()
            if not sym or not all(c.isalnum() or c in ".-" for c in sym) or len(sym) > 10:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, note, tags, added_ts) "
                "VALUES (?, '', ?, ?)",
                (sym, json.dumps([tag]), dt.datetime.now().isoformat(timespec="seconds")))
            added += cur.rowcount
        conn.commit()
        return added
    finally:
        conn.close()


@router.post("/watchlist/bulk")
def bulk_add(body: BulkAdd):
    import re
    symbols = re.split(r"[,\s;]+", body.symbols)
    added = _add_watch_symbols(symbols, "bulk-import")
    return {"ok": True, "added": added}


@router.post("/watchlist/import-schwab")
def import_schwab_watchlist():
    """One-time pull of Schwab watchlists — attempted honestly (the retail API
    may not expose them; the error says so and points at the fallbacks)."""
    from providers import mock as m
    fn = (m.fetch_schwab_watchlists if composer.use_mock()
          else (schwab.fetch_schwab_watchlists
                if registry.provider_name("account") == "schwab" else None))
    if fn is None:
        raise HTTPException(501, "Schwab is not the configured account provider")
    try:
        lists = fn()
    except ProviderError as e:
        raise HTTPException(502 if e.reason != "no_data" else 501, e.detail or e.reason)
    added = 0
    for wl in lists:
        added += _add_watch_symbols(wl["symbols"], f"schwab:{wl['name'][:20]}")
    return {"ok": True, "added": added,
            "lists": [{"name": w["name"], "count": len(w["symbols"])} for w in lists]}


@router.get("/discovery/report")
def discovery_report():
    """Printable Discover digest (same open-in-tab → auto-print flow as the
    analysis report)."""
    from analysis.discovery import list_candidates
    from api.report import build_discover_html
    return HTMLResponse(build_discover_html(list_candidates()))


@router.get("/discovery")
def discovery_list(include_dismissed: bool = Query(False)):
    from analysis.discovery import list_candidates
    return list_candidates(include_dismissed)


@router.post("/discovery/scan")
def discovery_scan():
    """Run all evidence screens on demand (each capped + defensive)."""
    from analysis import discovery as d
    m = registry.MODULES
    mockm = registry.use_mock()
    counts = {
        "pead": d.screen_pead(
            m["mock"].fetch_earnings_calendar_market if mockm else m["finnhub"].fetch_earnings_calendar_market,
            (m["mock"].fetch_fundamentals if mockm else m["finnhub"].fetch_fundamentals)),
        "insider": d.screen_insider_feed(
            m["mock"].fetch_form4_feed_counts if mockm else m["edgar"].fetch_form4_feed_counts),
        "short_interest": d.screen_short_interest(
            m["mock"].fetch_si_top_changes if mockm else m["finra"].fetch_si_top_changes),
    }
    return {"ok": True, "added": counts, "total_added": sum(counts.values())}


class DiscoveryPatch(BaseModel):
    status: str  # reviewed / dismissed / new


@router.patch("/discovery/{symbol}")
def discovery_patch(symbol: str, body: DiscoveryPatch):
    if body.status not in {"new", "reviewed", "dismissed"}:
        raise HTTPException(422, "status must be new|reviewed|dismissed")
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE discovery_candidates SET status=?, returned=0 WHERE symbol=?",
            (body.status, symbol.upper()))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "unknown candidate")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/discovery/{symbol}/promote")
def discovery_promote(symbol: str):
    """Candidate → watchlist (tagged 'discovery'), status=promoted."""
    sym = symbol.upper()
    conn = connect()
    try:
        row = conn.execute("SELECT reasons FROM discovery_candidates WHERE symbol=?",
                           (sym,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown candidate")
        reasons = json.loads(row["reasons"])
        note = (reasons[-1]["reason"][:120] if reasons else "")
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, note, tags, added_ts) VALUES (?,?,?,?)",
            (sym, note, json.dumps(["discovery"]),
             dt.datetime.now().isoformat(timespec="seconds")))
        conn.execute("UPDATE discovery_candidates SET status='promoted' WHERE symbol=?", (sym,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.get("/discovery/prompt")
def discovery_prompt():
    """The web-sweep prompt: paste into any agent; it writes discoveries/*.md
    which the watcher ingests (same file contract philosophy as discussions)."""
    day = dt.date.today().isoformat()
    return {"prompt": f"""# TickerLens discovery sweep — {day}
Research CURRENT sources (news, filings, sector coverage) for 8-15 NON-OBVIOUS
US stock/ETF candidates with early potential. EXCLUDE: Mag-7, S&P 500
mainstays, mega-cap household names, flagship index ETFs. For each: a concrete
thesis (what changed recently), and the main risk. No price predictions —
theses about businesses/flows/events only.

Write the result to `~/.tickerlens/discoveries/{day}.md` EXACTLY as:
```markdown
---
date: {day}
candidates:
  - ticker: XYZ
    thesis: <1-2 lines, concrete and recent>
    risk: <1 line>
  - ticker: ...
---
# Sweep notes {day}
<brief method/source notes>
```
The app ingests it automatically; duplicates merge by symbol."""}


@router.get("/lens/{symbol}")
def premium_lens(symbol: str):
    """Premium-seller's lens: implied vs empirical breach probability per
    short-strike candidate (validated DTE window only)."""
    from analysis.premium_lens import compose_lens
    from cache.store import get_or_fetch as _gof
    sym = symbol.upper()
    chain_fn = registry.resolve("option_chain_lens")
    if chain_fn is None:
        return {"available": False, "reason": "no options provider configured"}
    try:
        chain = _gof(f"lens_chain:{sym}", config.TTL["options"],
                     lambda: chain_fn(sym))["data"]
    except ProviderError as e:
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    bars = composer._bars_2y(sym, False) or []
    closes = [b["close"] for b in bars]
    earnings_days = None
    try:
        efn = registry.resolve("earnings")
        edata = _gof(f"earnings:{sym}", config.TTL["earnings"],
                     lambda: efn(sym))["data"] if efn else {}
        if edata.get("available"):
            earnings_days = edata.get("days_until")
    except ProviderError:
        pass
    return compose_lens(chain, closes, earnings_days)


@router.get("/vrp/{symbol}")
def vrp_report(symbol: str):
    """Accrued implied-move captures vs what realized — per-ticker variance
    risk premium. Thin until the app has been used a while; says so."""
    from cache.store import get_or_fetch as _gof
    sym = symbol.upper()
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT day, spot, dte, implied_pct FROM implied_move_history "
            "WHERE symbol = ? ORDER BY day", (sym,))]
    finally:
        conn.close()
    if not rows:
        return {"samples": [], "n_resolved": 0, "avg_ratio": None}
    try:
        bars = _gof(f"history2y:{sym}", config.TTL["history2y"],
                    lambda: _pv()["history"](sym, 520))["data"]
    except ProviderError:
        bars = []
    by_date = {b["date"]: b["close"] for b in bars}
    dates = sorted(by_date)
    out, ratios = [], []
    for r in rows:
        later = [d for d in dates if d > r["day"]]
        horizon = min(max(int(r["dte"] * 252 / 365), 1), len(later))
        realized = None
        if later and horizon >= 1 and len(later) >= horizon:
            realized = round(abs(by_date[later[horizon - 1]] / r["spot"] - 1) * 100, 2)
            if r["implied_pct"]:
                ratios.append(r["implied_pct"] / realized if realized > 0.05 else None)
        out.append({**r, "realized_pct": realized})
    valid = [x for x in ratios if x]
    return {"samples": out[-60:], "n_resolved": len(valid),
            "avg_ratio": round(sum(valid) / len(valid), 2) if valid else None,
            "note": "ratio >1 = options overpaid realized moves (seller-favorable), <1 = underpaid"}


class WhatIfBody(BaseModel):
    changes: dict[str, float]   # symbol → delta market value in $ (± ; cash auto-adjusts)


@router.post("/portfolio/whatif")
def portfolio_whatif(body: WhatIfBody):
    """Rebalance sandbox: hypothetical position changes → before/after risk +
    income. Pure preview; nothing is ever executed from this app."""
    from analysis.portfolio_risk import portfolio_risk
    from analysis.screener import portfolio_income
    from analysis import vol_bands as vb
    from cache.store import get_or_fetch as _gof
    try:
        account = composer.portfolio(force=False)["data"]
    except ProviderError as e:
        raise HTTPException(502, f"{e.reason}: {e.detail}")

    def closes_for(symbols: list[str]) -> dict[str, list[float]]:
        pv = _pv()
        out: dict[str, list[float]] = {}
        for s in symbols + ["SPY"]:
            try:
                bars = _gof(f"history:{s}", config.TTL["history"],
                            lambda s=s: pv["history"](s))["data"]
                out[s] = [b["close"] for b in vb.completed_bars(bars)]
            except ProviderError:
                continue
        return out

    def views(positions, cash):
        cmap = closes_for([p["symbol"] for p in positions])
        risk = portfolio_risk(positions, cmap, cash, cmap.get("SPY"))
        income = portfolio_income({"positions": positions,
                                   "total_value": sum(p["market_value"] for p in positions) + cash},
                                  _pv()["fundamentals"])
        return {"risk": risk, "income": {k: income[k] for k in
                ("projected_annual", "blended_yield_pct")}}

    before_pos = [dict(p) for p in account["positions"]]
    after_pos = {p["symbol"]: dict(p) for p in before_pos}
    cash_after = account.get("cash") or 0.0
    for sym, delta in body.changes.items():
        sym = sym.upper()
        cur = after_pos.get(sym) or {"symbol": sym, "market_value": 0.0}
        new_mv = max(0.0, cur["market_value"] + delta)
        cash_after -= (new_mv - cur["market_value"])   # buys consume cash, sells add
        cur["market_value"] = new_mv
        after_pos[sym] = cur
    if cash_after < 0:
        raise HTTPException(422, f"not enough cash: needs ${-cash_after:,.0f} more")
    after_list = [p for p in after_pos.values() if p["market_value"] > 0]
    return {"before": views(before_pos, account.get("cash") or 0.0),
            "after": views(after_list, cash_after),
            "cash_before": account.get("cash") or 0.0, "cash_after": round(cash_after, 2)}


@router.get("/portfolio/risk")
def get_portfolio_risk():
    """Roadmap #6: the account's own 80% range for tomorrow + diversification
    stats. Uses cached per-holding history (fetched on demand, TTL'd)."""
    from analysis.portfolio_risk import portfolio_risk
    from cache.store import get_or_fetch as _gof
    try:
        account = composer.portfolio(force=False)["data"]
    except ProviderError as e:
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    pv = _pv()
    closes_map: dict[str, list[float]] = {}
    for pos in account["positions"] + [{"symbol": "SPY"}]:
        sym = pos["symbol"]
        try:
            bars = _gof(f"history:{sym}", config.TTL["history"],
                        lambda s=sym: pv["history"](s), force=False)["data"]
            from analysis import vol_bands as vb
            closes_map[sym] = [b["close"] for b in vb.completed_bars(bars)]
        except ProviderError:
            continue
    return portfolio_risk(account["positions"], closes_map,
                          account.get("cash") or 0.0, closes_map.get("SPY"))


@router.get("/score-history/export")
def score_history_export():
    """Approved sharing path (LOGGED): a portable bundle of this instance's
    score history — for another machine/instance, never a silent cloud."""
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT symbol, ts, score, lean, components FROM setup_score_history")]
        conn.execute("INSERT INTO score_share_log (ts, direction, rows, detail) "
                     "VALUES (?, 'export', ?, 'api export')",
                     (dt.datetime.now().isoformat(timespec="seconds"), len(rows)))
        conn.commit()
    finally:
        conn.close()
    return {"format": "tickerlens-score-history-v1",
            "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
            "rows": rows}


class ScoreImportBody(BaseModel):
    rows: list[dict]


@router.post("/score-history/import")
def score_history_import(body: ScoreImportBody):
    """Import a bundle (deduped by symbol+ts). Logged, like export."""
    conn = connect()
    added = 0
    try:
        for r in body.rows:
            sym, ts = str(r.get("symbol", "")).upper(), str(r.get("ts", ""))
            if not sym or not ts or r.get("score") is None:
                continue
            dup = conn.execute("SELECT 1 FROM setup_score_history WHERE symbol=? AND ts=?",
                               (sym, ts)).fetchone()
            if dup:
                continue
            conn.execute("INSERT INTO setup_score_history (symbol, ts, score, lean, components) "
                         "VALUES (?,?,?,?,?)",
                         (sym, ts, float(r["score"]), str(r.get("lean", "NEUTRAL")),
                          str(r.get("components", "[]"))))
            added += 1
        conn.execute("INSERT INTO score_share_log (ts, direction, rows, detail) "
                     "VALUES (?, 'import', ?, ?)",
                     (dt.datetime.now().isoformat(timespec="seconds"), added,
                      f"received {len(body.rows)} rows"))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": added, "received": len(body.rows)}


@router.get("/score-history/share-log")
def score_share_log():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM score_share_log ORDER BY id DESC LIMIT 50")]
    finally:
        conn.close()


@router.get("/score-audit")
def get_score_audit():
    """Roadmap #10: your accumulated Setup Scores vs subsequent returns."""
    from analysis.score_audit import score_audit
    return score_audit()


@router.get("/settings/band-validation")
def get_band_validation():
    """The engine study shipped with the app (flat20 vs EWMA vs conformal)."""
    with open(config.BAND_VALIDATION_JSON) as f:
        return json.load(f)


class BandEngineBody(BaseModel):
    engine: str


@router.put("/settings/band-engine")
def put_band_engine(body: BandEngineBody):
    if body.engine not in config.BAND_ENGINES:
        raise HTTPException(422, f"engine must be one of {config.BAND_ENGINES}")
    conn = connect()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                     "VALUES ('band_engine', ?)", (json.dumps(body.engine),))
        conn.commit()
    finally:
        conn.close()
    invalidate("")  # bands recompute with the new engine on next analysis
    return {"ok": True, "engine": body.engine}


@router.get("/morning-sheet")
def morning_sheet():
    """Roadmap #8: one printable daily digest — screener, band breaks,
    earnings this week, portfolio day summary."""
    from fastapi.responses import HTMLResponse
    from analysis.screener import screen_watchlist
    from api.report import build_morning_html
    rows = screen_watchlist(_pv(), force=False)
    try:
        account = composer.portfolio(force=False)["data"]
    except ProviderError:
        account = None
    return HTMLResponse(build_morning_html(rows, account))


@router.get("/portfolio")
def get_portfolio(refresh: bool = Query(False)):
    """Live Schwab account positions (read-only, cached 5m) + organic value
    history. This tab supersedes the old standalone Portfolio app."""
    try:
        wrapped = composer.portfolio(force=refresh)
    except ProviderError as e:
        if e.reason == "auth_expired":
            raise HTTPException(503, f"Schwab auth: {e.detail} (run schwab-reauth)")
        raise HTTPException(502, f"{e.reason}: {e.detail}")
    return {
        **wrapped["data"],
        "source": wrapped["source"],
        "age_seconds": wrapped["age_seconds"],
        "history": composer.portfolio_history(),
    }


@router.get("/search")
def symbol_search(q: str = Query(min_length=2, max_length=40)):
    """Name → ticker lookup ("apple" → AAPL) for the search bar + ⌘K palette."""
    from cache.store import get_or_fetch

    fn = registry.resolve("symbol_search")
    if fn is None:
        return []
    try:
        wrapped = get_or_fetch(
            key=f"search:{q.strip().lower()}",
            ttl_seconds=config.TTL["search"],
            fetch_fn=lambda: fn(q),
        )
        return wrapped["data"]
    except ProviderError as e:
        # Search is a convenience — degrade to "no results" rather than a
        # scary error; direct ticker entry always still works.
        if e.reason == "rate_limited":
            raise HTTPException(429, "Search rate-limited — try again shortly")
        return []


@router.post("/sync")
def sync_all():
    """Nuke the provider cache; next analysis fetches everything live."""
    return {"ok": True, "invalidated": invalidate("")}


# ─── watchlist (TickerLens's own — independent of the Portfolio app's, Q2) ─────

class WatchAdd(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    note: str = ""
    tags: list[str] = []


class WatchPatch(BaseModel):
    note: str | None = None
    tags: list[str] | None = None


@router.get("/watchlist")
def get_watchlist():
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_ts DESC").fetchall()
        return [{**dict(r), "tags": json.loads(r["tags"] or "[]")} for r in rows]
    finally:
        conn.close()


@router.post("/watchlist", status_code=201)
def add_watch(body: WatchAdd):
    sym = body.symbol.upper().strip()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO watchlist (symbol, note, tags, added_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET note=excluded.note, tags=excluded.tags",
            (sym, body.note, json.dumps(body.tags),
             dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        return {"ok": True, "symbol": sym}
    finally:
        conn.close()


@router.patch("/watchlist/{symbol}")
def patch_watch(symbol: str, body: WatchPatch):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM watchlist WHERE symbol = ?",
                           (symbol.upper(),)).fetchone()
        if not row:
            raise HTTPException(404, "not on watchlist")
        note = body.note if body.note is not None else row["note"]
        tags = json.dumps(body.tags) if body.tags is not None else row["tags"]
        conn.execute("UPDATE watchlist SET note = ?, tags = ? WHERE symbol = ?",
                     (note, tags, symbol.upper()))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.delete("/watchlist/{symbol}")
def del_watch(symbol: str):
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "not on watchlist")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/watchlist/import-portfolio")
def import_portfolio_watchlist():
    """One-time convenience (Q2): copy symbols from the Portfolio app's
    watchlist (read-only). Existing TickerLens entries are left alone."""
    if not config.PORTFOLIO_DB_PATH:
        raise HTTPException(501, "no legacy portfolio DB configured (paths.portfolio_db in config.toml)")
    from providers import portfolio_db
    try:
        items = portfolio_db.fetch_watchlist_symbols()
    except ProviderError as e:
        raise HTTPException(503, f"Portfolio DB unavailable: {e.detail}")
    conn = connect()
    added = 0
    try:
        for item in items:
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, note, tags, added_ts) "
                "VALUES (?, ?, ?, ?)",
                (item["symbol"].upper(), item["note"], json.dumps(["from-portfolio"]),
                 dt.datetime.now().isoformat(timespec="seconds")))
            added += cur.rowcount
        conn.commit()
        return {"ok": True, "imported": added, "seen": len(items)}
    finally:
        conn.close()


# ─── discussions ───────────────────────────────────────────────────────────────

@router.get("/discussions")
def get_discussions(ticker: str | None = None, limit: int = Query(100, le=500)):
    return list_discussions(ticker, limit)


@router.get("/discussions/stream")
async def discussions_stream():
    """SSE: created/deleted/error events from the file watcher (Q5, deviation 1)."""
    return StreamingResponse(
        broadcaster.sse_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── glossary + settings + health ──────────────────────────────────────────────

@router.get("/glossary")
def get_glossary():
    with open(config.GLOSSARY_JSON) as f:
        data = json.load(f)
    return {"entries": data["entries"], "features": config.FEATURES}


class WeightsBody(BaseModel):
    weights: dict[str, float]


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='score_weights'").fetchone()
        weights = {**config.DEFAULT_WEIGHTS,
                   **(json.loads(row["value"]) if row else {})}
        return {"weights": weights, "defaults": config.DEFAULT_WEIGHTS,
                "score_bands": config.SCORE_BANDS,
                "disclaimer": config.SCORE_DISCLAIMER}
    finally:
        conn.close()


@router.put("/settings/weights")
def put_weights(body: WeightsBody):
    unknown = set(body.weights) - set(config.DEFAULT_WEIGHTS)
    if unknown:
        raise HTTPException(422, f"unknown components: {sorted(unknown)}")
    if any(v < 0 for v in body.weights.values()):
        raise HTTPException(422, "weights must be ≥ 0")
    if sum(body.weights.values()) <= 0:
        raise HTTPException(422, "at least one weight must be > 0")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('score_weights', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(body.weights),))
        conn.commit()
        return {"ok": True, "weights": {**config.DEFAULT_WEIGHTS, **body.weights}}
    finally:
        conn.close()


@router.get("/capabilities")
def capabilities():
    """What's wired to what — the frontend gates features on this."""
    return registry.snapshot()


@router.get("/health")
def health():
    """Per-provider status for the header dot + degradation banners (Q12)."""
    providers = {}
    for pname, module in registry.MODULES.items():
        if pname == "mock":
            continue
        try:
            providers[pname] = bool(module.health_check())
        except Exception:
            providers[pname] = False
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM claude_discussions").fetchone()["c"]
    finally:
        conn.close()
    return {"ok": True, "providers": providers, "discussions": n,
            "mock_mode": composer.use_mock()}
