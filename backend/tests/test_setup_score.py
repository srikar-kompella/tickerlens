"""
Setup Score unit tests — the densest suite by design (Q8b: pure functions,
every branch). Naming: test_<component>_<behavior>.
"""

import pytest

from analysis.setup_score import (
    COMPONENT_ORDER,
    c_analyst_trend,
    c_momentum,
    c_news_sentiment,
    c_options_positioning,
    c_social_buzz,
    compose,
    compute_from_sections,
    vol_conviction_factor,
)


# ─── c_momentum ────────────────────────────────────────────────────────────────

def test_momentum_zero_at_averages():
    assert c_momentum(0.0, 0.0) == 0.0


def test_momentum_positive_above_both():
    assert c_momentum(2.0, 3.0) > 0


def test_momentum_negative_below_both():
    assert c_momentum(-2.0, -3.0) < 0


def test_momentum_saturates_instead_of_pinning():
    # +30% above SMA20 (melt-up) must not exceed the [-1,1] envelope,
    # and the marginal effect must flatten (tanh):
    v_big, v_bigger = c_momentum(30.0, 30.0), c_momentum(60.0, 60.0)
    assert v_big <= 1.0 and v_bigger <= 1.0
    assert (v_bigger - v_big) < 0.01


def test_momentum_mixed_signals_partial():
    # above short-term but below medium-term → smallish net
    v = c_momentum(2.0, -2.0)
    assert -0.5 < v < 0.5


def test_momentum_weights_short_term_higher():
    assert c_momentum(3.0, 0.0) > c_momentum(0.0, 3.0)


def test_momentum_none_when_missing():
    assert c_momentum(None, 1.0) is None
    assert c_momentum(1.0, None) is None


# ─── c_news_sentiment ──────────────────────────────────────────────────────────

def test_news_balanced_is_zero():
    assert c_news_sentiment(0.5, 0.5, 40) == 0.0


def test_news_bullish_positive_bearish_negative():
    assert c_news_sentiment(0.8, 0.1, 40) > 0
    assert c_news_sentiment(0.1, 0.8, 40) < 0


def test_news_article_count_damps_thin_coverage():
    thin = c_news_sentiment(0.9, 0.1, 3)
    thick = c_news_sentiment(0.9, 0.1, 40)
    assert 0 < thin < thick
    assert thin == pytest.approx(thick * 3 / 20)


def test_news_zero_articles_zero_signal():
    assert c_news_sentiment(1.0, 0.0, 0) == 0.0


def test_news_full_weight_caps_at_twenty_articles():
    assert c_news_sentiment(0.7, 0.2, 20) == c_news_sentiment(0.7, 0.2, 500)


def test_news_none_when_missing():
    assert c_news_sentiment(None, 0.2, 10) is None
    assert c_news_sentiment(0.2, None, 10) is None


# ─── c_options_positioning ─────────────────────────────────────────────────────

def test_options_neutral_at_090():
    assert c_options_positioning(0.9) == 0.0


def test_options_low_pc_bullish_high_pc_bearish():
    assert c_options_positioning(0.5) > 0
    assert c_options_positioning(1.3) < 0


def test_options_clamped_at_extremes():
    assert c_options_positioning(0.1) == 1.0    # (0.9-0.1)/0.5 = 1.6 → clamp
    assert c_options_positioning(3.0) == -1.0


def test_options_exact_scaling():
    assert c_options_positioning(0.4) == pytest.approx(1.0)
    assert c_options_positioning(1.4) == pytest.approx(-1.0)


def test_options_none_when_missing():
    assert c_options_positioning(None) is None


# ─── c_analyst_trend ───────────────────────────────────────────────────────────

def _month(sb, b, h, s, ss):
    return {"strong_buy": sb, "buy": b, "hold": h, "sell": s, "strong_sell": ss,
            "total": sb + b + h + s + ss}


def test_analyst_upgrade_shift_positive():
    now, prev = _month(10, 10, 5, 1, 0), _month(5, 8, 10, 3, 0)
    assert c_analyst_trend([now, prev]) > 0


def test_analyst_downgrade_shift_negative():
    now, prev = _month(3, 5, 10, 6, 2), _month(10, 10, 5, 1, 0)
    assert c_analyst_trend([now, prev]) < 0


def test_analyst_shift_dominates_level():
    # same *level* this month; only history differs → the improving one scores higher
    now = _month(8, 8, 8, 2, 0)
    improving = c_analyst_trend([now, _month(2, 6, 14, 4, 0)])
    deteriorating = c_analyst_trend([now, _month(14, 8, 2, 0, 0)])
    assert improving > deteriorating


def test_analyst_single_month_uses_level_anchor_only():
    v = c_analyst_trend([_month(10, 10, 2, 0, 0)])
    assert 0 < v <= 0.5  # 0.5·net_now, no delta term


def test_analyst_none_on_empty_or_zero_totals():
    assert c_analyst_trend(None) is None
    assert c_analyst_trend([]) is None
    assert c_analyst_trend([_month(0, 0, 0, 0, 0)]) is None


def test_analyst_clamped():
    assert -1.0 <= c_analyst_trend([_month(30, 0, 0, 0, 0), _month(0, 0, 0, 0, 30)]) <= 1.0


# ─── c_social_buzz ─────────────────────────────────────────────────────────────

def test_social_polarity_sign_carries():
    assert c_social_buzz(0.8, 30) > 0
    assert c_social_buzz(-0.8, 30) < 0


def test_social_log_volume_scaling():
    quiet = c_social_buzz(1.0, 3)
    loud = c_social_buzz(1.0, 30)
    assert 0 < quiet < loud <= 1.0
    assert loud == pytest.approx(1.0, abs=0.02)  # ~30 msgs ≈ full weight


def test_social_zero_messages_zero_signal():
    assert c_social_buzz(1.0, 0) == 0.0


def test_social_none_when_missing():
    assert c_social_buzz(None, 10) is None


# ─── vol_conviction_factor (Q8a — multiplier, never a direction) ───────────────

def test_vol_factor_neutral_at_baseline():
    assert vol_conviction_factor(30.0, 30.0) == 1.0


def test_vol_factor_calm_boosts_turbulent_dampens():
    assert vol_conviction_factor(20.0, 30.0) > 1.0
    assert vol_conviction_factor(45.0, 30.0) < 1.0


def test_vol_factor_clamped():
    assert vol_conviction_factor(1.0, 100.0) == 1.15
    assert vol_conviction_factor(100.0, 1.0) == 0.85


def test_vol_factor_missing_data_is_exactly_one():
    assert vol_conviction_factor(None, 30.0) == 1.0
    assert vol_conviction_factor(30.0, None) == 1.0
    assert vol_conviction_factor(30.0, 0.0) == 1.0


# ─── compose ───────────────────────────────────────────────────────────────────

def _all(v: float) -> dict:
    return {k: v for k in COMPONENT_ORDER}


def test_compose_neutral_components_score_50():
    r = compose(_all(0.0))
    assert r["score"] == 50.0 and r["lean"] == "NEUTRAL"


def test_compose_all_bullish_maxes_all_bearish_mins():
    assert compose(_all(1.0))["score"] == 100.0
    assert compose(_all(-1.0))["score"] == 0.0


def test_compose_lean_thresholds():
    # tilt = 0.2 → 60 → BULLISH boundary; −0.2 → 40 → BEARISH boundary
    assert compose(_all(0.2))["lean"] == "BULLISH"
    assert compose(_all(-0.2))["lean"] == "BEARISH"
    assert compose(_all(0.19))["lean"] == "NEUTRAL"


def test_compose_missing_component_renormalizes_not_drags(  # Q8c — the ETF bug
):
    # all available components bullish; options/analyst/news missing (ETF-ish)
    comps = {"momentum": 0.6, "news_sentiment": None,
             "options_positioning": None, "analyst_trend": None,
             "social_buzz": 0.6}
    r = compose(comps)
    # naive missing=0 would give 50 + 50·(0.24) = 62 with full ETF drag;
    # renormalized it's 50 + 50·0.6 = 80 — missing data mustn't read as bearish
    assert r["score"] == 80.0
    assert r["components_available"] == 2


def test_compose_all_missing_is_flat_neutral():
    r = compose(_all(None))
    assert r["score"] == 50.0 and r["components_available"] == 0


def test_compose_vol_factor_scales_deviation_only():
    base = compose(_all(0.4))["score"]                     # 70
    boosted = compose(_all(0.4), vol_factor=1.15)["score"]
    damped = compose(_all(0.4), vol_factor=0.85)["score"]
    assert boosted == pytest.approx(50 + (base - 50) * 1.15, abs=0.1)
    assert damped == pytest.approx(50 + (base - 50) * 0.85, abs=0.1)
    # and it can never flip the sign of the tilt:
    assert (boosted - 50) * (base - 50) > 0


def test_compose_points_sum_to_score_offset():
    comps = {"momentum": 0.5, "news_sentiment": -0.3, "options_positioning": 0.2,
             "analyst_trend": 0.1, "social_buzz": -0.8}
    r = compose(comps, vol_factor=1.1)
    assert sum(row["points"] for row in r["components"]) == pytest.approx(
        r["score"] - 50.0, abs=0.3)  # rounding tolerance across 5 rows


def test_compose_custom_weights_respected():
    comps = {"momentum": 1.0, "news_sentiment": -1.0, "options_positioning": None,
             "analyst_trend": None, "social_buzz": None}
    # momentum-only weighting → pure bullish
    r = compose(comps, weights={"momentum": 100.0, "news_sentiment": 0.0,
                                "options_positioning": 0.0, "analyst_trend": 0.0,
                                "social_buzz": 0.0})
    assert r["score"] == 100.0
    # zero-weighted news must be reported as unavailable-for-scoring
    news_row = next(x for x in r["components"] if x["component"] == "news_sentiment")
    assert news_row["points"] == 0.0


def test_compose_score_clamped_to_0_100():
    assert 0.0 <= compose(_all(1.0), vol_factor=1.15)["score"] <= 100.0


def test_compose_carries_disclaimer_always():
    assert "does not predict" in compose(_all(0.0))["disclaimer"]


def test_compose_rows_cover_every_component_in_order():
    rows = compose(_all(0.1))["components"]
    assert [r["component"] for r in rows] == COMPONENT_ORDER


# ─── compute_from_sections (payload adapter) ───────────────────────────────────

def test_adapter_handles_all_sections_none():
    r = compute_from_sections(None, None, None, None, None)
    assert r["score"] == 50.0 and r["components_available"] == 0


def test_adapter_respects_available_false():
    news = {"available": False, "bullish_pct": 0.9, "bearish_pct": 0.0,
            "articles_week": 50}
    r = compute_from_sections({"sma20_dist": 0, "sma50_dist": 0,
                               "rv_20d": 30, "rv_60d": 30}, news, None, None, None)
    row = next(x for x in r["components"] if x["component"] == "news_sentiment")
    assert row["available"] is False


def test_adapter_full_wiring_end_to_end():
    signals = {"sma20_dist": 3.0, "sma50_dist": 5.0, "rv_20d": 20.0, "rv_60d": 30.0}
    news = {"available": True, "bullish_pct": 0.8, "bearish_pct": 0.1, "articles_week": 40}
    options = {"available": True, "put_call_ratio": 0.5}
    recs = {"available": True, "months": [_month(10, 10, 5, 1, 0), _month(5, 8, 10, 3, 0)]}
    social = {"available": True, "polarity": 0.7, "total_msgs": 30}
    r = compute_from_sections(signals, news, options, recs, social)
    assert r["score"] > 60 and r["lean"] == "BULLISH"
    assert r["components_available"] == 5
    assert r["vol_factor"] > 1.0  # calm regime boost was applied
