"""
End-to-end tests: strategy_brain_s2 (spot_fv_disloc) fires and skips correctly.

The new S2 prices a Bachelier digital on the CURRENT spot vs strike and trades only
when the de-vigged market mid is stale-cheap relative to it (anchored-EV gate). No
momentum, no contract velocity, no OBI. Direction comes from the model.
"""
import sys, os, time, collections
from unittest.mock import patch
import contextlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s2, _S2_FV_CONFIG


@pytest.fixture(autouse=True)
def _restore_prices():
    """Snapshot and restore the per-asset price deques mutated by these tests."""
    saved = {a: asset_manager._prices.get(a) for a in ("BTC", "ETH", "SOL", "XRP", "DOGE")}
    saved_btc = bot_state.btc_prices
    yield
    for a, dq in saved.items():
        if dq is not None:
            asset_manager._prices[a] = dq
    bot_state.btc_prices = saved_btc


def _seed_spot(asset: str, last_price: float, strike_side_above: bool, strike: float):
    """
    Seed the asset's price deque with ~78s of prints all on one side of the strike,
    ending at last_price. Guarantees the spot-sign confirmation gate is satisfied.
    """
    now = time.time()
    dq = collections.deque(maxlen=2000)
    # Prints ramp toward last_price but stay on the correct side of the strike.
    if strike_side_above:
        start = max(last_price * 0.999, strike * 1.001)
    else:
        start = min(last_price * 1.001, strike * 0.999)
    for i in range(40):
        t = now - (39 - i) * 2.0
        frac = i / 39.0
        dq.append((t, start + (last_price - start) * frac))
    asset_manager._prices[asset] = dq
    if asset == "BTC":
        bot_state.btc_prices = dq
    return dq


def _run_s2(asset, spot, strike, yes_ask, no_ask, secs_left=600.0, cfg_extra=None):
    config = {"mode": "paper", "quiet_hours_enabled": False}
    if cfg_extra:
        config.update(cfg_extra)
    above = spot >= strike
    _seed_spot(asset, spot, above, strike)
    ticker = f"KXMOCK-{asset}-T{int(strike) if strike >= 1 else strike}"
    with patch("bot_strategy.read_config", return_value=config):
        return strategy_brain_s2(
            btc_price=spot, strike=strike,
            yes_ask=yes_ask, no_ask=no_ask,
            elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
            ticker=ticker, asset=asset,
        )


class TestS2FvFires:
    """S2 fires when the spot is a real distance past strike and the ask is stale-cheap."""

    @pytest.mark.parametrize("asset,strike,spot", [
        ("BTC", 60000.0, 60350.0),
        ("SOL", 150.0, 150.7),
        ("XRP", 2.30, 2.313),
        ("DOGE", 0.150, 0.1512),
    ])
    def test_fires_yes_when_spot_above_and_ask_cheap(self, asset, strike, spot):
        """Spot clearly above strike + a stale-cheap YES ask → trade YES."""
        r = _run_s2(asset, spot, strike, yes_ask=40.0, no_ask=58.0)
        assert r["action"] == "trade", f"{asset} should trade: {r['reasoning']}"
        assert r["side"] == "yes"
        assert r["signals"]["market_edge"] >= 0.035
        # The RAW Bachelier fair value (always P(YES)) must favor YES when spot is above.
        assert r["signals"]["model_raw_p_yes"] >= 0.50

    def test_fires_no_when_spot_below_and_ask_cheap(self):
        """Spot clearly below strike + a stale-cheap NO ask → trade NO."""
        r = _run_s2("SOL", spot=149.3, strike=150.0, yes_ask=58.0, no_ask=40.0)
        assert r["action"] == "trade", f"S2 should trade NO: {r['reasoning']}"
        assert r["side"] == "no"
        assert r["signals"]["market_edge"] >= 0.035


class TestS2FvSkips:
    """S2 must skip when the fair-value dislocation is absent or the book is bad."""

    def test_skips_low_z(self):
        """Spot barely past strike → |z| below the conviction gate → skip."""
        r = _run_s2("SOL", spot=150.02, strike=150.0, yes_ask=40.0, no_ask=58.0)
        assert r["action"] == "skip"
        assert "s2_fv_lowz" in r["reasoning"]

    def test_skips_when_mid_already_fair(self):
        """Spot above strike but the mid already prices it in → no edge → EV-gate skip."""
        # Fair ~0.72 but the ask sits near fair (yes_ask 70) → market not stale → skip.
        r = _run_s2("SOL", spot=150.7, strike=150.0, yes_ask=70.0, no_ask=31.0)
        assert r["action"] == "skip"
        assert "s2_ev_gate" in r["reasoning"] or "s2_fv_wide_spread" in r["reasoning"]

    def test_skips_wide_spread(self):
        """A wide round-trip spread (high vig) must skip even with a real dislocation."""
        r = _run_s2("SOL", spot=150.7, strike=150.0, yes_ask=40.0, no_ask=68.0)
        assert r["action"] == "skip"
        assert "s2_fv_wide_spread" in r["reasoning"]

    def test_skips_spot_flicker(self):
        """When the last prints straddle the strike, the sign-confirmation gate skips."""
        now = time.time()
        dq = collections.deque(maxlen=2000)
        for i in range(40):
            # Oscillate across the strike on the final prints.
            p = 150.0 + (0.6 if i % 2 == 0 else -0.6)
            dq.append((now - (39 - i) * 2.0, p))
        asset_manager._prices["SOL"] = dq
        with patch("bot_strategy.read_config", return_value={"mode": "paper", "quiet_hours_enabled": False}):
            r = strategy_brain_s2(150.6, 150.0, 40.0, 58.0, 300.0, 600.0, "KXSOL-FLICK", asset="SOL")
        assert r["action"] == "skip"
        assert "s2_fv_flicker" in r["reasoning"] or "s2_fv_lowz" in r["reasoning"]

    def test_eth_disabled_by_default(self):
        """ETH is disabled in S2 by default."""
        r = _run_s2("ETH", spot=3020.0, strike=3000.0, yes_ask=40.0, no_ask=58.0)
        assert r["action"] == "skip"
        assert "s2_fv_disabled" in r["reasoning"]

    def test_eth_enabled_via_config(self):
        """ETH can be re-enabled via s2_eth_enabled."""
        r = _run_s2("ETH", spot=3020.0, strike=3000.0, yes_ask=40.0, no_ask=58.0,
                    cfg_extra={"s2_eth_enabled": True})
        assert r["reasoning"] != "s2_fv_disabled:ETH"

    def test_time_gate_final_90s(self):
        """S2 must skip in the final 90s (settlement-auction tail)."""
        r = _run_s2("SOL", spot=150.7, strike=150.0, yes_ask=40.0, no_ask=58.0, secs_left=60.0)
        assert r["action"] == "skip"
        assert "s2_time_gate" in r["reasoning"]


def test_s2_config_present_for_enabled_assets():
    """The new S2 fair-value config must cover BTC/SOL/XRP/DOGE."""
    for a in ("BTC", "SOL", "XRP", "DOGE"):
        assert a in _S2_FV_CONFIG
        assert _S2_FV_CONFIG[a]["min_z"] > 0
