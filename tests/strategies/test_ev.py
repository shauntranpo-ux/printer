import pytest
from strategies.ev import compute_bidirectional_ev


def test_positive_ev_yes_side():
    # Model says 0.70, market yes_ask=60c (market says 0.60)
    # yes_ev = 0.70 - 0.60 - small_fee > 0
    result = compute_bidirectional_ev(
        p_model=0.70,
        yes_ask_cents=60,
        no_ask_cents=42,  # market tight
        stake_dollars=5.0,
    )
    assert result.best_side == "yes"
    assert result.best_ev > 0


def test_positive_ev_no_side():
    # Model says 0.30, yes_ask=60c (market says 0.60)
    # Model disagrees; NO side wins.
    result = compute_bidirectional_ev(
        p_model=0.30,
        yes_ask_cents=60,
        no_ask_cents=42,
        stake_dollars=5.0,
    )
    assert result.best_side == "no"
    assert result.best_ev > 0


def test_no_positive_ev_returns_none():
    # Model matches market exactly, fees push both negative
    result = compute_bidirectional_ev(
        p_model=0.60,
        yes_ask_cents=60,
        no_ask_cents=42,
        stake_dollars=5.0,
    )
    assert result.best_side is None


def test_yes_ev_and_no_ev_not_derived_from_each_other():
    # Pass asymmetric yes_ask and no_ask; confirm EVs reflect actual prices
    result = compute_bidirectional_ev(
        p_model=0.50,
        yes_ask_cents=55,  # yes priced at 0.55
        no_ask_cents=50,   # no priced at 0.50 (wide market, 5c spread)
        stake_dollars=5.0,
    )
    # yes_ev = 0.50 - 0.55 - fee = negative
    # no_ev = 0.50 - 0.50 - fee = near zero or slightly negative
    # no_ev > yes_ev because we used actual no_ask not derived
    assert result.no_ev > result.yes_ev


def test_ev_edge_case_yes_price_zero_or_one():
    # Garbage input: yes_ask = 0
    result = compute_bidirectional_ev(
        p_model=0.70,
        yes_ask_cents=0,
        no_ask_cents=50,
        stake_dollars=5.0,
    )
    # Yes is degenerate; no side should be evaluated
    assert result.yes_ev == -float("inf")


def test_ev_includes_fee():
    # Maker fee is lower -> higher EV than taker
    p = 0.80
    result_taker = compute_bidirectional_ev(p, 60, 42, 5.0, maker=False)
    result_maker = compute_bidirectional_ev(p, 60, 42, 5.0, maker=True)
    assert result_maker.yes_ev > result_taker.yes_ev
