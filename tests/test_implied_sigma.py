"""Market-implied sigma: quote back-out, acceptance filters, EWMA, and _sigma_eff paths."""
import sys, os, math, time
from statistics import NormalDist
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs

CFG = {"settlement_avg_seconds": 0}


@pytest.fixture(autouse=True)
def _clean():
    bot_state._implied_sigma.clear()
    bot_state._sigma_scale.clear()
    bot_state._basis_offsets.clear()
    yield
    bot_state._implied_sigma.clear()
    bot_state._sigma_scale.clear()
    bot_state._basis_offsets.clear()


def _quote_from_sigma(spot, strike, secs, sigma, spread=2.0):
    """Build a two-sided book whose de-vigged mid matches Phi(ln(spot/strike)/(sigma*sqrt(t)))."""
    ps = sigma * math.sqrt(secs / 900.0)
    p_yes = NormalDist().cdf(math.log(spot / strike) / ps)
    mid_c = p_yes * 100.0
    return mid_c + spread / 2.0, (100.0 - mid_c) + spread / 2.0


def test_backout_recovers_known_sigma():
    yes_ask, no_ask = _quote_from_sigma(100.0, 100.3, 480.0, 0.0045)
    got = bs._implied_sigma_from_quote("SOL", 100.0, 100.3, yes_ask, no_ask, 480.0, CFG)
    assert got == pytest.approx(0.0045, abs=2e-4)


def test_backout_rejects_wide_spread():
    yes_ask, no_ask = _quote_from_sigma(100.0, 100.3, 480.0, 0.0045, spread=5.0)
    assert bs._implied_sigma_from_quote("SOL", 100.0, 100.3, yes_ask, no_ask, 480.0, CFG) is None


def test_backout_rejects_extreme_mid():
    # Deep-ITM mid (>0.90) carries almost no vol information.
    yes_ask, no_ask = _quote_from_sigma(100.0, 99.0, 480.0, 0.0045)
    assert bs._implied_sigma_from_quote("SOL", 100.0, 99.0, yes_ask, no_ask, 480.0, CFG) is None


def test_backout_rejects_near_coinflip_z():
    # Mid ~0.52 -> |z| < 0.2: the inversion is numerically unstable there.
    assert bs._implied_sigma_from_quote("SOL", 100.0, 100.0005, 52.5, 48.5, 480.0, CFG) is None


def test_backout_rejects_sign_mismatch():
    # Spot BELOW strike but the mid prices YES above 0.5: that IS a dislocation -
    # exactly the observation that must not pollute the vol anchor.
    assert bs._implied_sigma_from_quote("SOL", 99.7, 100.0, 76.0, 26.0, 480.0, CFG) is None


def test_backout_rejects_out_of_time_window():
    yes_ask, no_ask = _quote_from_sigma(100.0, 100.3, 480.0, 0.0045)
    assert bs._implied_sigma_from_quote("SOL", 100.0, 100.3, yes_ask, no_ask, 100.0, CFG) is None
    assert bs._implied_sigma_from_quote("SOL", 100.0, 100.3, yes_ask, no_ask, 800.0, CFG) is None


def test_backout_rejects_insane_sigma():
    # A quote implying > 5x the static base is a broken book, not a vol observation.
    yes_ask, no_ask = _quote_from_sigma(100.0, 103.0, 480.0, 0.04)
    assert bs._implied_sigma_from_quote("SOL", 100.0, 103.0, yes_ask, no_ask, 480.0, CFG) is None


def test_ewma_updates_and_counts():
    yes_ask, no_ask = _quote_from_sigma(100.0, 100.3, 480.0, 0.0045)
    for _ in range(3):
        bs.update_implied_sigma("SOL", 100.0, 100.3, yes_ask, no_ask, 480.0, CFG)
    st = bot_state._implied_sigma["SOL"]
    assert st["n"] == 3
    assert st["sigma"] == pytest.approx(0.0045, abs=3e-4)
    # A very different observation moves the EWMA by at most the 0.25 alpha cap.
    y2, n2 = _quote_from_sigma(100.0, 100.3, 480.0, 0.009)
    before = st["sigma"]
    bs.update_implied_sigma("SOL", 100.0, 100.3, y2, n2, 480.0, CFG)
    after = bot_state._implied_sigma["SOL"]["sigma"]
    assert after > before
    assert after <= before + 0.25 * (0.009 - before) + 1e-9


def test_sigma_eff_anchors_to_fresh_implied():
    bot_state._implied_sigma["SOL"] = {"sigma": 0.004, "ts": time.time(), "n": 5}
    out = bs._sigma_eff("SOL", {"sigma_implied_max_age_secs": 900})
    assert 0.6 * 0.004 <= out <= 1.7 * 0.004


def test_sigma_eff_cold_start_without_anchor():
    base = bs._ASSET_VOL_15M["SOL"]
    with patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        out = bs._sigma_eff("SOL", {})
    assert bs._FLOOR_MULT * base <= out <= bs._CEIL_MULT * base


def test_sigma_eff_ignores_stale_anchor():
    base = bs._ASSET_VOL_15M["SOL"]
    bot_state._implied_sigma["SOL"] = {"sigma": 0.04, "ts": time.time() - 5000, "n": 9}
    with patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        out = bs._sigma_eff("SOL", {})
    assert out <= bs._CEIL_MULT * base   # the 0.04 anchor must NOT leak through


def test_sigma_eff_applies_fitted_scale():
    bot_state._implied_sigma["SOL"] = {"sigma": 0.004, "ts": time.time(), "n": 5}
    ref = bs._sigma_eff("SOL", {})
    bot_state._sigma_scale["SOL"] = 0.5
    assert bs._sigma_eff("SOL", {}) == pytest.approx(0.5 * ref)


def test_brains_emit_descaled_z(monkeypatch):
    """signals.z_raw must equal z * applied scale so the refit target is stationary."""
    import collections
    saved = asset_manager._prices.get("SOL")
    try:
        now = time.time()
        dq = collections.deque(
            [(now - (39 - i) * 2.0, 150.30 + 0.14 * (i / 39.0)) for i in range(40)],
            maxlen=2000)
        asset_manager._prices["SOL"] = dq
        bot_state._sigma_scale["SOL"] = 0.8
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False,
                                 "calibration_enabled": False, "auto_gate_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            # Late window + favorite mid ~0.77 so S2 reaches the model stage and emits z_raw.
            r = bs.strategy_brain_s2(150.44, 150.0, 78.0, 24.0, 660.0, 240.0,
                                     "KXSOL-ZRAW", asset="SOL")
        sig = r["signals"]
        assert sig["z_raw"] == pytest.approx(sig["z"] * 0.8)
    finally:
        if saved is not None:
            asset_manager._prices["SOL"] = saved


def test_tracking_state_prune():
    import collections
    import bot_loops
    now = time.time()
    bot_state._contract_mid_history["KX-OLD"] = collections.deque([(now - 3600, 50.0)])
    bot_state._contract_mid_history["KX-LIVE"] = collections.deque([(now - 30, 50.0)])
    bot_state._contract_price_history["KX-OLD2"] = collections.deque([(now - 3600, 40.0)])
    bot_loops._maker_track_last_fetch["KX-OLD3"] = now - 3600
    bot_loops._prune_tracking_state()
    assert "KX-OLD" not in bot_state._contract_mid_history
    assert "KX-LIVE" in bot_state._contract_mid_history
    assert "KX-OLD2" not in bot_state._contract_price_history
    assert "KX-OLD3" not in bot_loops._maker_track_last_fetch
    bot_state._contract_mid_history.clear()


def test_calibration_restore_round_trip(monkeypatch):
    import bot_loops
    import scripts.calibration as cal
    now = time.time()
    payload = {
        "sigma_scale": {"SOL": 0.7, "XRP": 9.0},           # 9.0 out of range: dropped
        "implied_sigma": {
            "SOL": {"sigma": 0.0041, "ts": now - 100, "n": 7},
            "XRP": {"sigma": 0.0041, "ts": now - 90000, "n": 7},  # too old: dropped
        },
    }
    monkeypatch.setattr(cal, "load_calibration", lambda *a, **k: payload)
    bot_loops._load_saved_calibration()
    assert bot_state._sigma_scale.get("SOL") == pytest.approx(0.7)
    assert "XRP" not in bot_state._sigma_scale
    assert bot_state._implied_sigma.get("SOL", {}).get("sigma") == pytest.approx(0.0041)
    assert "XRP" not in bot_state._implied_sigma
