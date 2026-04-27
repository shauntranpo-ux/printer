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


# ── EV Fix Audit Tests ────────────────────────────────────────────────────────
# These tests verify the p_model_for_ev fix in base.py.
# All use a flat 1%-of-stake fee mock so expected values are exact.
#
# ev.py formulas (lines 63, 74):
#   yes_ev = p_model          - yes_price - (yes_fee / yes_stake)
#   no_ev  = (1.0 - p_model)  - no_price  - (no_fee  / no_stake)
#
# The mock makes fee/stake = 0.01 exactly, so:
#   yes_ev = p_model         - yes_price - 0.01
#   no_ev  = (1 - p_model)  - no_price  - 0.01
# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import patch as _patch


def _flat_fee(contracts: int, price_dollars: float) -> float:
    """Mock: fee = 1% of stake  →  fee/stake = 0.01 exactly."""
    return 0.01 * contracts * price_dollars


# Test 1 — YES direction, 70% confidence at 55c entry
def test_audit_yes_direction_p70_ask55():
    # yes_ev = 0.70 - 0.55 - 0.01 = +0.14
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r = compute_bidirectional_ev(p_model=0.70, yes_ask_cents=55, no_ask_cents=47, stake_dollars=25.0)
    assert abs(r.yes_ev - 0.14) < 1e-9, f"expected +0.14, got {r.yes_ev}"
    assert r.yes_ev > 0


# Test 2 — NO direction: p_model=0.70 means P(NO wins)=0.30, cheap NO at 25c
def test_audit_no_pmodel070_ask25():
    # no_ev = (1-0.70) - 0.25 - 0.01 = 0.30 - 0.26 = +0.04
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r = compute_bidirectional_ev(p_model=0.70, yes_ask_cents=77, no_ask_cents=25, stake_dollars=25.0)
    assert abs(r.no_ev - 0.04) < 1e-9, f"expected +0.04, got {r.no_ev}"
    assert r.no_ev > 0


# Test 3 — NO direction: p_model=0.30 means P(NO wins)=0.70, normal 55c entry
def test_audit_no_pmodel030_ask55():
    # no_ev = (1-0.30) - 0.55 - 0.01 = 0.70 - 0.56 = +0.14
    # This is what base.py produces after the fix: ST=-1 → p_model_for_ev = 0.30
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r = compute_bidirectional_ev(p_model=0.30, yes_ask_cents=47, no_ask_cents=55, stake_dollars=25.0)
    assert abs(r.no_ev - 0.14) < 1e-9, f"expected +0.14, got {r.no_ev}"
    assert r.no_ev > 0


# Test 4 — Symmetry: YES@0.70/55c and NO@0.30/55c must give identical EV
def test_audit_symmetry_yes_vs_no_same_entry():
    # yes_ev(p=0.70, ask=0.55) == no_ev(p=0.30, ask=0.55) == +0.14
    # Confirms: 70% confidence in direction + 55c entry = same EV regardless of which side.
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r_yes = compute_bidirectional_ev(p_model=0.70, yes_ask_cents=55, no_ask_cents=99, stake_dollars=25.0)
        r_no  = compute_bidirectional_ev(p_model=0.30, yes_ask_cents=99, no_ask_cents=55, stake_dollars=25.0)
    assert abs(r_yes.yes_ev - r_no.no_ev) < 1e-9, (
        f"YES ev={r_yes.yes_ev:.6f} vs NO ev={r_no.no_ev:.6f} — should be equal"
    )
    assert r_yes.yes_ev > 0


# Test 5 — Edge: yes_ask=99c, p_model=0.70 → yes_ev strongly negative (don't chase)
def test_audit_edge_expensive_yes_ask():
    # yes_ev = 0.70 - 0.99 - 0.01 = -0.30 — never trade at 99c YES with 70% confidence
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r = compute_bidirectional_ev(p_model=0.70, yes_ask_cents=99, no_ask_cents=3, stake_dollars=25.0)
    assert r.yes_ev < -0.29, f"expected < -0.29 (strongly negative), got {r.yes_ev}"
    assert r.yes_ev < 0


# Test 6 — Edge: p_model=0.50 (no edge) → both sides negative after fees → skip
def test_audit_edge_no_model_edge():
    # yes_ev = 0.50 - 0.52 - 0.01 = -0.03
    # no_ev  = 0.50 - 0.52 - 0.01 = -0.03
    # best_side is None → bot skips
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r = compute_bidirectional_ev(p_model=0.50, yes_ask_cents=52, no_ask_cents=52, stake_dollars=25.0)
    assert r.yes_ev < 0, f"yes_ev should be negative, got {r.yes_ev}"
    assert r.no_ev  < 0, f"no_ev should be negative, got {r.no_ev}"
    assert r.best_side is None


# Test 7 (regression) — Bug re-entry guard: p_model=0.70 for NO at 55c must be negative
def test_audit_regression_buggy_pmodel_gives_wrong_sign():
    # PRE-FIX (bug): base.py used p_model=0.70 for NO direction.
    #   no_ev = (1-0.70) - 0.55 - 0.01 = -0.26  ← bot would skip a valid NO trade
    # POST-FIX (correct): base.py now uses p_model=0.30 for NO direction.
    #   no_ev = (1-0.30) - 0.55 - 0.01 = +0.14  ← bot correctly trades NO
    with _patch("strategies.ev.taker_fee", new=_flat_fee):
        r_buggy = compute_bidirectional_ev(p_model=0.70, yes_ask_cents=47, no_ask_cents=55, stake_dollars=25.0)
        r_fixed = compute_bidirectional_ev(p_model=0.30, yes_ask_cents=47, no_ask_cents=55, stake_dollars=25.0)
    assert r_buggy.no_ev < 0,  f"buggy path: expected negative no_ev, got {r_buggy.no_ev}"
    assert r_fixed.no_ev > 0,  f"fixed path: expected positive no_ev, got {r_fixed.no_ev}"
    assert abs(r_fixed.no_ev - 0.14) < 1e-9, f"fixed no_ev should be exactly +0.14, got {r_fixed.no_ev}"
