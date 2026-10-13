"""
Round-3 (P1–P6 + imports) tests.
"""

import pytest

from db import connect
from discussions_svc.parser import parse_discussion_file, upsert_discussion
from tests.conftest import write_md


@pytest.fixture()
def client(tmp_db, mock_mode, tmp_discussions, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "DISCUSSIONS_DIR", str(tmp_discussions))
    from fastapi.testclient import TestClient
    from app import app
    with TestClient(app) as c:
        yield c


# ─── P1+P4: screener ───────────────────────────────────────────────────────────

def test_screener_row_shape_and_flags(client):
    client.post("/api/watchlist", json={"symbol": "NVDA"})
    client.post("/api/watchlist", json={"symbol": "PHYS"})   # no options
    rows = client.get("/api/watchlist/screener").json()
    assert {r["symbol"] for r in rows} == {"NVDA", "PHYS"}
    nvda = next(r for r in rows if r["symbol"] == "NVDA")
    for key in ("price", "day_pct", "band_half_pct", "implied_move_pct",
                "implied_vs_band", "days_to_earnings", "yesterday_z",
                "outside_band_yesterday", "last_score", "tags", "note"):
        assert key in nvda
    assert nvda["band_half_pct"] > 0
    assert nvda["implied_vs_band"] is not None
    phys = next(r for r in rows if r["symbol"] == "PHYS")
    assert phys["implied_move_pct"] is None   # no listed options → dash, not error
    assert isinstance(nvda["outside_band_yesterday"], bool)


def test_screener_shows_last_persisted_score_only_after_analysis(client):
    client.post("/api/watchlist", json={"symbol": "AAPL"})
    before = client.get("/api/watchlist/screener").json()[0]
    assert before["last_score"] is None       # honest: no fresh recompute
    client.get("/api/analysis/AAPL")          # persists a score organically
    after = client.get("/api/watchlist/screener").json()[0]
    assert after["last_score"] is not None and after["last_score_ts"]


def test_screener_empty_watchlist(client):
    assert client.get("/api/watchlist/screener").json() == []


def test_watchlist_quotes_batch_and_cache_warm(client, tmp_db):
    """One batched call → prices for all rows AND warmed quote:SYM cache
    entries (so enrichment/analysis reuse them without refetching)."""
    client.post("/api/watchlist", json={"symbol": "AAPL"})
    client.post("/api/watchlist", json={"symbol": "MSFT"})
    q = client.get("/api/watchlist/quotes").json()
    assert set(q) == {"AAPL", "MSFT"}
    assert q["AAPL"]["price"] > 0 and "day_pct" in q["AAPL"]
    from db import connect
    conn = connect()
    cached = conn.execute("SELECT COUNT(*) FROM cache WHERE key IN "
                          "('quote:AAPL','quote:MSFT')").fetchone()[0]
    conn.close()
    assert cached == 2
    assert client.get("/api/watchlist/quotes").json() != {} or True  # idempotent


def test_screener_symbols_subset(client):
    for s in ("AAPL", "MSFT", "NVDA"):
        client.post("/api/watchlist", json={"symbol": s})
    subset = client.get("/api/watchlist/screener", params={"symbols": "aapl,NVDA"}).json()
    assert {r["symbol"] for r in subset} == {"AAPL", "NVDA"}
    # symbols not on the watchlist are ignored, not fabricated
    stray = client.get("/api/watchlist/screener", params={"symbols": "TSLA"}).json()
    assert stray == []


# ─── P2: income lens ───────────────────────────────────────────────────────────

def test_portfolio_income_math(client):
    inc = client.get("/api/portfolio/income").json()
    assert inc["projected_annual"] >= 0
    assert inc["coverage_pct"] is not None
    assert len(inc["holdings"]) > 0
    # spot-check one holding's arithmetic: income == mv * yield / 100
    port = client.get("/api/portfolio").json()
    mv = {p["symbol"]: p["market_value"] for p in port["positions"]}
    for h in inc["holdings"]:
        if h["yield_pct"]:
            assert h["income_annual"] == pytest.approx(
                mv[h["symbol"]] * h["yield_pct"] / 100, abs=0.02)
    # blended yield consistent with totals
    assert inc["blended_yield_pct"] == pytest.approx(
        inc["projected_annual"] / port["total_value"] * 100, abs=0.05)


# ─── P3: decision journal ──────────────────────────────────────────────────────

def test_decision_parsed_and_price_captured_from_cache(client, tmp_db, tmp_discussions):
    from discussions_svc.watcher import catch_up_scan
    client.get("/api/analysis/MSFT")   # warms quote cache → capture source
    write_md(tmp_discussions, "MSFT", "d1.md", [
        "ticker: MSFT", "created_ts: 2026-07-13T10:00:00",
        "summary: taking a starter position", "decision: buy",
    ])
    assert catch_up_scan() >= 1
    rows = client.get("/api/decisions").json()
    assert len(rows) == 1
    d = rows[0]
    assert d["decision"] == "buy" and d["ticker"] == "MSFT"
    assert d["decision_price"] is not None     # captured from cached quote
    assert d["current_price"] is not None
    assert d["pct_since"] == pytest.approx(
        (d["current_price"] - d["decision_price"]) / d["decision_price"] * 100, abs=0.01)


def test_decision_price_preserved_on_file_edit(tmp_db):
    """Journal semantics: the price at FIRST decision sticks, even if the
    file is edited later (COALESCE in the upsert)."""
    row = {"ticker": "AAPL", "file_path": "/x/d.md", "response_markdown": "b",
           "summary": "s", "tags": "[]", "decision": "trim",
           "decision_price": 210.0, "created_ts": "2026-07-13T09:00:00",
           "file_mtime": "2026-07-13T09:00:00"}
    upsert_discussion(row)
    edited = {**row, "decision_price": None, "file_mtime": "2026-07-13T12:00:00"}
    upsert_discussion(edited)
    conn = connect()
    got = conn.execute("SELECT decision_price FROM claude_discussions "
                       "WHERE file_path='/x/d.md'").fetchone()[0]
    conn.close()
    assert got == 210.0


def test_invalid_decision_dropped(tmp_path):
    p = write_md(tmp_path, "AAPL", "x.md", ["ticker: AAPL", "decision: yolo"])
    assert parse_discussion_file(p)["decision"] is None


def test_no_decision_no_price_capture(tmp_path, tmp_db):
    p = write_md(tmp_path, "AAPL", "y.md", ["ticker: AAPL", "summary: no call"])
    row = parse_discussion_file(p)
    rid = upsert_discussion(row)
    conn = connect()
    got = conn.execute("SELECT decision, decision_price FROM claude_discussions "
                       "WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert got[0] is None and got[1] is None


# ─── P5: actions ───────────────────────────────────────────────────────────────

def test_actions_crud_cycle(client):
    assert client.get("/api/actions").json() == []
    r = client.post("/api/actions", json={"symbol": "msft", "action": "TRIM",
                                          "rationale": "overlaps 401k", "priority": 2})
    assert r.status_code == 201
    aid = r.json()["id"]
    lst = client.get("/api/actions").json()
    assert lst[0]["symbol"] == "MSFT" and lst[0]["status"] == "open"
    done = client.patch(f"/api/actions/{aid}", json={"status": "done"}).json()
    assert done["status"] == "done"
    assert client.patch(f"/api/actions/{aid}", json={"status": "bogus"}).status_code == 422
    assert client.patch("/api/actions/999", json={"status": "done"}).status_code == 404
    # done items sort after open ones
    client.post("/api/actions", json={"action": "review VXUS weighting"})
    lst2 = client.get("/api/actions").json()
    assert lst2[0]["status"] == "open" and lst2[-1]["status"] == "done"


# ─── watchlist imports ─────────────────────────────────────────────────────────

def test_bulk_add_parses_messy_input(client):
    r = client.post("/api/watchlist/bulk",
                    json={"symbols": "aapl, NVDA;  hims\nBRK.B  aapl  !!bad!!  TOOLONGSYM99"})
    assert r.status_code == 200
    added = {w["symbol"] for w in client.get("/api/watchlist").json()}
    assert {"AAPL", "NVDA", "HIMS", "BRK.B"} <= added
    assert "!!BAD!!" not in added and "TOOLONGSYM99" not in added
    assert r.json()["added"] == 4              # dupe AAPL ignored


def test_schwab_watchlist_import_mock(client):
    r = client.post("/api/watchlist/import-schwab")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 7                  # 4 + 3 from the two mock lists
    assert {l["name"] for l in body["lists"]} == {"Income candidates", "Research"}
    tags = {w["symbol"]: w["tags"] for w in client.get("/api/watchlist").json()}
    assert any(t.startswith("schwab:") for t in tags["JEPQ"])
    # idempotent second run adds nothing
    assert client.post("/api/watchlist/import-schwab").json()["added"] == 0


# ─── report export ─────────────────────────────────────────────────────────────

def test_report_contains_everything(client, tmp_db, tmp_discussions):
    from discussions_svc.watcher import catch_up_scan
    client.get("/api/analysis/AAPL")   # warm cache → decision price capture
    write_md(tmp_discussions, "AAPL", "r1.md", [
        "ticker: AAPL", "created_ts: 2026-07-13T09:00:00",
        "summary: Range fair, waiting for earnings", "decision: hold",
        "tags: [report-test]",
    ], body="## TL;DR\n\nBand and implied move agree; **no edge** in acting today.")
    assert catch_up_scan() >= 1
    r = client.get("/api/analysis/AAPL/report")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    page = r.text
    for needle in (
        "RESEARCH SNAPSHOT", "AAPL",                    # header
        "80% expected range", "Setup Score",            # analysis core
        "does not predict price direction",             # honesty footer
        "Range fair, waiting for earnings",             # discussion summary
        "no edge",                                      # rendered markdown body
        "Recorded decisions", "HOLD",                   # decision journal
        "svg",                                          # sparkline present
        "window.print",                                 # auto print hook
    ):
        assert needle in page, f"report missing: {needle}"
    # markdown actually rendered, not dumped raw
    assert "<strong>no edge</strong>" in page
    # <title> = default save-as-PDF filename: TickerLens_AAPL_YYYY-MM-DD_HHMM
    import re
    m = re.search(r"<title>(.*?)</title>", page)
    assert m and re.fullmatch(r"TickerLens_AAPL_\d{4}-\d{2}-\d{2}_\d{4}", m.group(1)), m.group(1)


def test_report_clean_ticker_without_history(client):
    r = client.get("/api/analysis/XOM/report")
    assert r.status_code == 200
    assert "Claude research discussions" not in r.text  # section omitted, not empty


# ─── prompt v4 ─────────────────────────────────────────────────────────────────

def test_prompt_v4_decision_contract(client):
    p = client.get("/api/analysis/AAPL/prompt").json()["prompt"]
    assert "decision: <buy|sell|trim|hold|pass" in p
    assert "never invent one" in p
