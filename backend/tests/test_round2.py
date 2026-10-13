"""
Round-2 feature tests: live portfolio, news fallback chain, Claude news view,
discussion ordering by activity, prompt v3 contract.
"""

import pytest

from analysis import composer
from analysis.news_lex import score_headlines, score_text
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


# ─── headline lexicon (pure) ───────────────────────────────────────────────────

def test_lex_scores_direction():
    assert score_text("AAPL beats estimates, shares surge on record profit") > 0
    assert score_text("AAPL plunges after lawsuit and layoffs warning") < 0
    assert score_text("AAPL to hold annual developer conference") == 0


def test_lex_aggregate_split():
    heads = [
        {"headline": "X beats estimates", "summary": ""},
        {"headline": "X announces buyback and dividend hike", "summary": ""},
        {"headline": "X faces probe", "summary": ""},
        {"headline": "X schedules meeting", "summary": ""},
    ]
    agg = score_headlines(heads)
    assert agg["available"] and agg["method"] == "headline-lexicon"
    assert agg["bullish_pct"] == 0.5      # 2 of 4
    assert agg["bearish_pct"] == 0.25     # 1 of 4
    assert agg["articles_week"] == 4
    assert agg["per_headline"] == [1, 1, -1, 0]


def test_lex_empty_is_unavailable():
    assert score_headlines([]) == {"available": False}


# ─── portfolio (mock account) ──────────────────────────────────────────────────

def test_portfolio_endpoint_shape_and_snapshot(client, tmp_db):
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    p = r.json()
    assert p["total_value"] > 0 and p["cash"] == 4060.0
    assert p["positions"][0]["market_value"] >= p["positions"][-1]["market_value"]
    for pos in p["positions"]:
        assert {"symbol", "qty", "market_value", "gain", "day_pl", "weight_pct"} <= set(pos)
    # a LIVE fetch appended exactly one organic snapshot; cached fetch doesn't
    assert len(p["history"]) == 1
    p2 = client.get("/api/portfolio").json()
    assert len(p2["history"]) == 1
    assert p2["source"] == "cache"


def test_ownership_chip_now_reads_live_account(tmp_db, mock_mode):
    a = composer.analyze("MSFT")           # MSFT is in the mock account
    assert a["position"]["owned"] is True
    assert a["position"]["qty"] > 0
    b = composer.analyze("XOM")            # XOM is not
    assert b["position"]["owned"] is False


# ─── news fallback chain ───────────────────────────────────────────────────────

def test_news_uses_finnhub_when_available(tmp_db, mock_mode):
    a = composer.analyze("AAPL")           # mock news_sentiment is available
    assert a["news_sentiment"]["available"]
    assert a["news_sentiment"]["method"] == "finnhub"


def test_news_falls_back_to_headline_lexicon(tmp_db, mock_mode, monkeypatch):
    from providers import mock as m
    monkeypatch.setattr(m, "fetch_news_sentiment", lambda s: {"available": False})
    a = composer.analyze("TSLA")
    assert a["news_sentiment"]["method"] == "headline-lexicon"
    assert a["sections"]["news_sentiment"]["derived_from"] == "headlines"
    # and the score still has a live news component driven by the fallback
    news_row = next(c for c in a["setup_score"]["components"]
                    if c["component"] == "news_sentiment")
    assert news_row["available"] is True


# ─── Claude news view (frontmatter → analysis payload) ─────────────────────────

def test_claude_news_view_parsed_and_surfaced(client, tmp_db, tmp_discussions):
    from discussions_svc.watcher import catch_up_scan
    write_md(tmp_discussions, "NVDA", "a.md", [
        "ticker: NVDA", "created_ts: 2026-07-12T10:00:00",
        "summary: news check", "news_view: Bearish",
        "news_note: Export-control headline dominating coverage",
    ])
    assert catch_up_scan() >= 1
    a = client.get("/api/analysis/NVDA").json()
    assert a["claude_news"]["news_view"] == "bearish"      # normalized
    assert "Export-control" in a["claude_news"]["news_note"]


def test_invalid_news_view_dropped(tmp_path):
    p = write_md(tmp_path, "AAPL", "x.md",
                 ["ticker: AAPL", "news_view: to-the-moon"])
    assert parse_discussion_file(p)["news_view"] is None


# ─── discussion ordering by last activity (ask #2) ─────────────────────────────

def test_discussions_ordered_by_activity_not_created(client, tmp_db):
    def row(path, created, mtime):
        return {"ticker": "AAPL", "file_path": path, "prompt": None,
                "response_markdown": "b", "summary": path, "tags": "[]",
                "context_hash": None, "context_snapshot": None,
                "news_view": None, "news_note": None,
                "created_ts": created, "file_mtime": mtime}
    upsert_discussion(row("/x/1.md", "2026-07-01T09:00:00", "2026-07-01T09:00:00"))
    upsert_discussion(row("/x/2.md", "2026-07-10T09:00:00", "2026-07-10T09:00:00"))
    # oldest discussion edited today → must surface FIRST
    upsert_discussion(row("/x/1.md", "2026-07-01T09:00:00", "2026-07-12T18:00:00"))
    got = [d["file_path"] for d in
           client.get("/api/discussions", params={"ticker": "AAPL"}).json()]
    assert got == ["/x/1.md", "/x/2.md"]


# ─── prompt v3 contract ────────────────────────────────────────────────────────

def test_prompt_v3_new_file_mandate_and_news_check(client):
    p = client.get("/api/analysis/AAPL/prompt").json()["prompt"]
    assert "ALWAYS create a NEW file per discussion" in p
    assert "news_view:" in p and "news_note:" in p
    assert "check of TODAY'S news" in p
