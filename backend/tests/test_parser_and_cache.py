"""
Discussion parser + DB ops + TTL cache tests.
"""
import json
import time

import pytest

from cache.store import get_or_fetch, invalidate
from discussions_svc.parser import (
    ParseError,
    delete_discussion,
    list_discussions,
    parse_discussion_file,
    upsert_discussion,
)
from providers.base import ProviderError
from tests.conftest import write_md


# ─── parser ────────────────────────────────────────────────────────────────────

def test_parse_full_frontmatter(tmp_path):
    p = write_md(tmp_path, "AAPL", "2026-07-13T14:32:15.md", [
        "ticker: AAPL",
        "created_ts: 2026-07-13T14:32:15",
        "summary: Earnings look priced in",
        "tags: [earnings-review, technical]",
        "context_hash: sha256:abc123",
    ], body="# Take\n\nRange looks fair.")
    row = parse_discussion_file(p)
    assert row["ticker"] == "AAPL"
    assert row["created_ts"].startswith("2026-07-13T14:32:15")
    assert row["summary"] == "Earnings look priced in"
    assert json.loads(row["tags"]) == ["earnings-review", "technical"]
    assert row["context_hash"] == "sha256:abc123"
    assert "Range looks fair." in row["response_markdown"]


def test_parse_ticker_falls_back_to_folder(tmp_path):
    p = write_md(tmp_path, "NVDA", "x.md", ["summary: from folder"])
    assert parse_discussion_file(p)["ticker"] == "NVDA"


def test_parse_summary_falls_back_to_first_body_line(tmp_path):
    p = write_md(tmp_path, "TSLA", "x.md", ["ticker: TSLA"],
                 body="## The real takeaway here\n\nmore text")
    assert parse_discussion_file(p)["summary"] == "The real takeaway here"


def test_parse_tags_accepts_comma_string(tmp_path):
    p = write_md(tmp_path, "META", "x.md", ["ticker: META", "tags: alpha, beta"])
    assert json.loads(parse_discussion_file(p)["tags"]) == ["alpha", "beta"]


def test_parse_missing_created_ts_uses_mtime(tmp_path):
    p = write_md(tmp_path, "COIN", "x.md", ["ticker: COIN"])
    row = parse_discussion_file(p)
    assert row["created_ts"]  # ISO string from mtime, never empty


def test_parse_empty_body_raises(tmp_path):
    folder = tmp_path / "AAPL"
    folder.mkdir()
    p = folder / "empty.md"
    p.write_text("---\nticker: AAPL\n---\n\n")
    with pytest.raises(ParseError, match="empty body"):
        parse_discussion_file(str(p))


def test_parse_underscore_folder_no_ticker_raises(tmp_path):
    folder = tmp_path / "_failed"
    folder.mkdir()
    p = folder / "x.md"
    p.write_text("---\nsummary: hm\n---\nbody")
    with pytest.raises(ParseError, match="no ticker"):
        parse_discussion_file(str(p))


def test_parse_garbage_yaml_raises(tmp_path):
    folder = tmp_path / "AAPL"
    folder.mkdir()
    p = folder / "bad.md"
    p.write_text("---\n: : :\n  bad yaml: [unclosed\n---\nbody")
    with pytest.raises(ParseError):
        parse_discussion_file(str(p))


# ─── upsert / delete / list round-trip ─────────────────────────────────────────

def _row(path, ticker="AAPL", summary="s1"):
    return {"ticker": ticker, "file_path": path, "prompt": None,
            "response_markdown": "body", "summary": summary, "tags": "[]",
            "context_hash": None, "context_snapshot": None,
            "news_view": None, "news_note": None,
            "created_ts": "2026-07-12T10:00:00", "file_mtime": "2026-07-12T10:00:00"}


def test_upsert_insert_then_update_same_path(tmp_db):
    rid1 = upsert_discussion(_row("/x/AAPL/a.md", summary="first"))
    rid2 = upsert_discussion(_row("/x/AAPL/a.md", summary="edited"))
    assert rid1 == rid2  # Q5b: edits update in place, no duplicate rows
    rows = list_discussions("AAPL")
    assert len(rows) == 1 and rows[0]["summary"] == "edited"


def test_delete_returns_identity_then_none(tmp_db):
    upsert_discussion(_row("/x/AAPL/gone.md"))
    removed = delete_discussion("/x/AAPL/gone.md")
    assert removed and removed["ticker"] == "AAPL"
    assert delete_discussion("/x/AAPL/gone.md") is None  # idempotent
    assert list_discussions("AAPL") == []


def test_list_filters_by_ticker_and_orders_desc(tmp_db):
    r1 = _row("/x/AAPL/1.md"); r1["created_ts"] = "2026-07-10T09:00:00"
    r2 = _row("/x/AAPL/2.md"); r2["created_ts"] = "2026-07-12T09:00:00"
    r3 = _row("/x/NVDA/3.md", ticker="NVDA")
    for r in (r1, r2, r3):
        upsert_discussion(r)
    aapl = list_discussions("AAPL")
    assert [d["file_path"] for d in aapl] == ["/x/AAPL/2.md", "/x/AAPL/1.md"]
    assert len(list_discussions()) == 3
    assert isinstance(aapl[0]["tags"], list)  # JSON decoded for the API


# ─── cache ─────────────────────────────────────────────────────────────────────

def test_cache_miss_then_fresh_hit(tmp_db):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"v": calls["n"]}

    first = get_or_fetch("t:AAPL", 60, fetch)
    second = get_or_fetch("t:AAPL", 60, fetch)
    assert first["source"] == "live" and second["source"] == "cache"
    assert second["data"] == {"v": 1} and calls["n"] == 1


def test_cache_expiry_refetches(tmp_db):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    get_or_fetch("t:X", 0, fetch)          # ttl=0 → immediately stale
    time.sleep(0.01)
    out = get_or_fetch("t:X", 0, fetch)
    assert out["source"] == "live" and calls["n"] == 2


def test_cache_force_bypasses_freshness(tmp_db):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    get_or_fetch("t:Y", 3600, fetch)
    out = get_or_fetch("t:Y", 3600, fetch, force=True)
    assert out["source"] == "live" and calls["n"] == 2


def test_cache_serves_stale_on_provider_error(tmp_db):
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise ProviderError("rate_limited", "429")
        return {"good": True}

    get_or_fetch("t:Z", 0, fetch)
    state["fail"] = True
    time.sleep(0.01)
    out = get_or_fetch("t:Z", 0, fetch)
    assert out["source"] == "stale"
    assert out["data"] == {"good": True}
    assert out["error_reason"] == "rate_limited"
    assert out["age_seconds"] >= 0


def test_cache_force_still_falls_back_to_stale(tmp_db):
    def ok():
        return 1

    def boom():
        raise ProviderError("unavailable", "down")

    get_or_fetch("t:F", 3600, ok)
    out = get_or_fetch("t:F", 3600, boom, force=True)  # refresh click must not lose data
    assert out["source"] == "stale" and out["data"] == 1


def test_cache_raises_when_no_stale_available(tmp_db):
    def boom():
        raise ProviderError("unavailable", "down")

    with pytest.raises(ProviderError):
        get_or_fetch("t:none", 60, boom)


def test_cache_invalidate_by_prefix(tmp_db):
    get_or_fetch("quote:AAPL", 3600, lambda: 1)
    get_or_fetch("quote:NVDA", 3600, lambda: 2)
    get_or_fetch("social:AAPL", 3600, lambda: 3)
    assert invalidate("quote:") == 2
    # social survives; quotes refetch
    assert get_or_fetch("social:AAPL", 3600, lambda: 99)["source"] == "cache"
    assert get_or_fetch("quote:AAPL", 3600, lambda: 42)["data"] == 42
