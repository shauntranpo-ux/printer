"""Step 2: Bachelier digital pricer, live realized-vol sigma, S1 harness visibility, basis."""
import collections
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import asset_manager
import bot_state
import bot_strategy as s


@pytest.fixture(autouse=True)
def _restore_prices():
    """These tests reassign asset_manager._prices entries; restore them so later
    test modules (e.g. test_s2_fires) don't see polluted ETH spot data."""
    saved = dict(asset_manager._prices)
    yield
    asset_manager._prices.clear()
    asset_manager._prices.update(saved)


# _bachelier_p_above
def test_pricer_at_strike_is_half():
    assert abs(s._bachelier_p_above(100, 100, 300, 0.008) - 0.5) < 1e-12


def test_pricer_monotone_and_symmetric():
    above = s._bachelier_p_above(101, 100, 300, 0.008)
    below = s._bachelier_p_above(99, 100, 300, 0.008)
    assert above > 0.5 > below
    # log-symmetry: p(K*r) + p(K/r) ~ 1 for the mirrored moneyness
    r = 1.01
    assert abs(s._bachelier_p_above(100 * r, 100, 300, 0.008)
               + s._bachelier_p_above(100 / r, 100, 300, 0.008) - 1.0) < 1e-9


def test_pricer_deep_otm_short_time_saturates_uncapped():
    # uncapped pure pricer must reach ~1.0 (the 0.85 clamp lives in the caller)
    assert s._bachelier_p_above(110, 100, 5, 0.008) > 0.99


def test_pricer_guards():
    assert s._bachelier_p_above(0, 100, 300, 0.008) == 0.5
    assert s._bachelier_p_above(100, 0, 300, 0.008) == 0.5
    # secs_left -> 0 with spot above strike resolves to a hard 1.0
    assert s._bachelier_p_above(101, 100, 0, 1e-9) == 1.0


def test_pricer_matches_linear_form_for_small_moves():
    # exact log digital ~ the shipped linearized z=dist/period_sigma for sub-1.5% moves
    spot, strike, secs, sig = 100.6, 100.0, 300.0, 0.008
    period = sig * math.sqrt(secs / 900.0)
    z_lin = (spot - strike) / strike / period
    p_lin = 0.5 * (1 + math.erf(z_lin / math.sqrt(2)))
    assert abs(s._bachelier_p_above(spot, strike, secs, sig) - p_lin) < 1e-3


# _live_sigma_15m
def _seed(asset, pairs):
    dq = collections.deque(maxlen=2000)
    for ts, p in pairs:
        dq.append((ts, p))
    asset_manager._prices[asset] = dq


def test_live_sigma_thin_data_falls_back_to_static():
    asset_manager._prices["ETH"] = collections.deque(maxlen=2000)
    static = s._ASSET_VOL_15M["ETH"] * s._time_of_day_vol_multiplier()
    assert abs(s._live_sigma_15m("ETH") - static) < 1e-12


def test_live_sigma_recovers_known_value_from_60s_bars():
    # |r|=0.002 per 60s bar -> 15m sigma = 0.002*sqrt(900/60) = 0.002*sqrt(15) ~ 0.007746
    now = time.time()
    price, pairs = 2000.0, []
    for i in range(40):
        price *= math.exp(0.002 * ((-1) ** i))
        pairs.append((now - (40 - i) * 60, price))
    _seed("ETH", pairs)
    assert abs(s._live_sigma_15m("ETH") - 0.002 * math.sqrt(15)) < 5e-4


def test_live_sigma_winsorizes_fat_finger():
    # clean 60s bars + one 0.3% jump at dt=2s must NOT blow sigma up (>10%)
    now = time.time()
    price, pairs = 2000.0, []
    for i in range(40):
        price *= math.exp(0.002 * ((-1) ** i))
        pairs.append((now - (40 - i) * 60, price))
    _seed("ETH", list(pairs))
    clean = s._live_sigma_15m("ETH")
    pairs.append((pairs[-1][0] + 2, pairs[-1][1] * 1.003))  # fat-finger tick at dt=2s
    _seed("ETH", pairs)
    dirty = s._live_sigma_15m("ETH")
    assert abs(dirty - clean) / clean < 0.10, f"fat-finger inflated sigma: {clean}->{dirty}"


def test_live_sigma_output_clamped_to_band():
    base = s._ASSET_VOL_15M["ETH"]
    # all-tiny moves -> would underflow, but clamp floors at 0.5x base
    now = time.time()
    pairs = [(now - (40 - i) * 60, 2000.0 * (1 + 1e-6 * ((-1) ** i))) for i in range(40)]
    _seed("ETH", pairs)
    sig = s._live_sigma_15m("ETH")
    assert s._FLOOR_MULT * base <= sig <= s._CEIL_MULT * base


def test_live_sigma_no_tod_double_count(monkeypatch):
    # live path must NOT multiply by ToD again (static fallback already has it)
    monkeypatch.setattr(s, "_time_of_day_vol_multiplier", lambda: 2.0)
    now = time.time()
    price, pairs = 2000.0, []
    for i in range(40):
        price *= math.exp(0.002 * ((-1) ** i))
        pairs.append((now - (40 - i) * 60, price))
    _seed("ETH", pairs)
    # live estimate ~ 0.007746 regardless of ToD=2.0 (no double count); clamp ceiling is 2x base
    sig = s._live_sigma_15m("ETH")
    assert sig <= s._CEIL_MULT * s._ASSET_VOL_15M["ETH"] + 1e-9
    assert abs(sig - 0.002 * math.sqrt(15)) < 1e-3


# S1 harness visibility
def test_s1_dicts_carry_model_raw_p_yes():
    import inspect
    src = inspect.getsource(s.strategy_brain_s1)
    # both S1 return dicts that reach the model stage (trade, ev-gate skip) expose model_raw_p_yes
    assert src.count("model_raw_p_yes") >= 2, "S1 must emit model_raw_p_yes for the edge harness"


# settlement basis
def test_record_settlement_basis():
    import bot_loops
    bot_state._settlement_basis.clear()
    bot_loops._record_settlement_basis("KXETH-A", "ETH", 2000.0, 2010.0, "yes", True)  # agree
    bot_loops._record_settlement_basis("KXETH-B", "ETH", 2000.0, 1990.0, "yes", True)  # disagree
    bot_loops._record_settlement_basis("KXETH-C", "ETH", 2000.0, 2010.0, "yes", False)  # not official -> skip
    samples = list(bot_state._settlement_basis)
    assert len(samples) == 2
    assert samples[0]["agree"] is True and samples[1]["agree"] is False
    # never raises on bad strike
    bot_loops._record_settlement_basis("X", "ETH", 0.0, 1.0, "yes", True)
