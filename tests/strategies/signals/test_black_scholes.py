"""Unit tests for compute_bs_p_yes."""
import math
import pytest

from strategies.signals.black_scholes import compute_bs_p_yes

def test_none_on_zero_price():       assert compute_bs_p_yes(0.0, 100.0, 0.001, 600.0) is None
def test_none_on_zero_strike():      assert compute_bs_p_yes(100.0, 0.0, 0.001, 600.0) is None
def test_none_on_zero_seconds():     assert compute_bs_p_yes(100.0, 100.0, 0.001, 0.0) is None
def test_none_on_negative_seconds(): assert compute_bs_p_yes(100.0, 100.0, 0.001, -1.0) is None
def test_none_on_zero_vol():         assert compute_bs_p_yes(100.0, 100.0, 0.0, 600.0) is None

def test_atm_approximately_half():
    p = compute_bs_p_yes(100.0, 100.0, 0.001, 600.0)
    assert p is not None and abs(p - 0.5) < 0.01

def test_deep_itm_near_one():
    p = compute_bs_p_yes(200.0, 100.0, 0.001, 600.0)
    assert p is not None and p > 0.99

def test_deep_otm_near_zero():
    p = compute_bs_p_yes(50.0, 100.0, 0.001, 600.0)
    assert p is not None and p < 0.01

def test_monotone_in_current_price():
    prices = [96.0, 98.0, 100.0, 102.0, 104.0]
    probs = [compute_bs_p_yes(s, 100.0, 0.005, 600.0) for s in prices]
    assert all(p is not None for p in probs)
    assert all(probs[i] < probs[i + 1] for i in range(len(probs) - 1))

def test_output_in_unit_interval():
    p = compute_bs_p_yes(100.0, 95.0, 0.002, 300.0)
    assert p is not None and 0.0 <= p <= 1.0
