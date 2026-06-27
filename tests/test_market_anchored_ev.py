"""Market-anchored EV framework: de-vigged mid, shrinkage cap, YES/NO symmetry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _market_implied_p_yes, _anchored_ev, _kalshi_fee_frac


def test_market_implied_p_yes_devigs_both_asks():
    # yes_ask=32, no_ask=70 -> yes_bid=30 -> mid=31 -> 0.31
    assert abs(_market_implied_p_yes(32, 70) - 0.31) < 1e-9
    # symmetric book at 50/50
    assert abs(_market_implied_p_yes(50, 50) - 0.50) < 1e-9


def test_market_implied_p_yes_rejects_bad_books():
    assert _market_implied_p_yes(0, 70) is None
    assert _market_implied_p_yes(32, 0) is None
    assert _market_implied_p_yes(None, 70) is None
    assert _market_implied_p_yes(100, 70) is None


def test_anchored_ev_caps_runaway_model_edge():
    # Model screams 0.58 on a 0.31 market; cap 0.08 -> traded prob capped at 0.39.
    ev, p_side, mkt, edge = _anchored_ev("yes", 32, 70, 0.58, 0.08)
    assert abs(edge - 0.08) < 1e-9          # edge capped at the shrinkage limit
    assert abs(p_side - 0.39) < 1e-9        # 0.31 mid + 0.08 cap
    assert ev < 0.08                        # net of spread + fee, EV is realistic/bounded


def test_anchored_ev_yes_no_symmetry():
    # A YES book (yes_ask=32,no_ask=70) is the mirror of a NO view of the same book.
    res_yes = _anchored_ev("yes", 32, 70, 0.58, 0.08)
    res_no = _anchored_ev("no", 32, 70, 0.20, 0.08)  # p_yes 0.20 -> p_no 0.80
    # both saturate the cap and yield comparable (small) positive edge
    assert abs(res_yes[3] - res_no[3]) < 1e-9
    assert res_no[1] > res_no[2]            # model p_side above market p_side for NO too


def test_anchored_ev_negative_when_no_edge():
    # Model agrees with the market mid (no edge) -> EV is negative after spread+fee.
    ev, p_side, mkt, edge = _anchored_ev("yes", 32, 70, 0.31, 0.08)
    assert abs(edge) < 1e-9
    assert ev < 0


def test_anchored_ev_none_on_bad_book():
    assert _anchored_ev("yes", 0, 70, 0.5, 0.08) is None


def test_kalshi_fee_formula():
    # 7% fee rate * p * (1-p): at p=0.30 -> 0.07*0.3*0.7 = 0.0147
    assert abs(_kalshi_fee_frac(0.30) - 0.0147) < 1e-9
    assert _kalshi_fee_frac(0.0) == 0.0
    assert _kalshi_fee_frac(1.0) == 0.0
