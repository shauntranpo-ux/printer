"""
End-to-end tests: strategy_brain_s2 (spot_fv_disloc) fires and skips correctly.

S2 prices a Bachelier digital on the CURRENT spot vs strike and trades only when the
de-vigged market mid is stale-cheap relative to it - within the too-good-to-be-true
band, off the longshot tail, and (when mid history exists) only against a book that
lagged a fresh spot move. Fixtures pin the ToD multiplier to 1.0 and use secs_left=480
(8 min, inside the 2.5-9.0 entry window); fair values are computed against the
re-fitted static vol table (SOL 0.0046 etc.), z ~ 0.9 -> fair ~ 0.816.
"""
import sys, os, time, collections
from unittest.mock import patch

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
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    bot_state._contract_mid_history.clear()
    yield
    for a, dq in saved.items():
        if dq is not None:
            asset_manager._prices[a] = dq
    bot_state.btc_prices = saved_btc
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    bot_state._contract_mid_history.clear()


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


def _run_s2(asset, spot, strike, yes_ask, no_ask, secs_left=480.0, cfg_extra=None):
    config = {"mode": "paper", "quiet_hours_enabled": False}
    if cfg_extra:
        config.update(cfg_extra)
    above = spot >= strike
    _seed_spot(asset, spot, above, strike)
    ticker = f"KXMOCK-{asset}-T{int(strike) if strike >= 1 else strike}"
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        return strategy_brain_s2(
            btc_price=spot, strike=strike,
            yes_ask=yes_ask, no_ask=no_ask,
            elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
            ticker=ticker, asset=asset,
        )


class TestS2FvFires:
    """S2 fires when the spot is a real distance past strike and the mid lags fair."""

    # Spots chosen so z ~ 0.9 under the static sigma at 8 min left (fair ~ 0.816);
    # asks 73/29 put the de-vigged mid at 0.72 -> gap ~ 0.10, inside the TGTBT band.
    @pytest.mark.parametrize("asset,strike,spot", [
        ("BTC", 60000.0, 60088.0),
        ("SOL", 150.0, 150.44),
        ("XRP", 2.30, 2.3063),
        ("DOGE", 0.150, 0.15048),
    ])
    def test_fires_yes_when_spot_above_and_ask_cheap(self, asset, strike, spot):
        """Spot clearly above strike + a lagging cheap YES ask -> trade YES."""
        r = _run_s2(asset, spot, strike, yes_ask=73.0, no_ask=29.0)
        assert r["action"] == "trade", f"{asset} should trade: {r['reasoning']}"
        assert r["side"] == "yes"
        assert r["signals"]["market_edge"] >= 0.04
        assert r["signals"]["gap"] <= 0.15
        # The RAW Bachelier fair value (always P(YES)) must favor YES when spot is above.
        assert r["signals"]["model_raw_p_yes"] >= 0.50
        # No mid history seeded -> the staleness gate passes fail-open and says so.
        assert r["signals"]["freshness"] == "unknown"
        assert "sigma_eff" in r["signals"] and "z" in r["signals"] and "spot" in r["signals"]

    def test_fires_no_when_spot_below_and_ask_cheap(self):
        """Spot clearly below strike + a lagging cheap NO ask -> trade NO."""
        r = _run_s2("SOL", spot=149.561, strike=150.0, yes_ask=29.0, no_ask=73.0)
        assert r["action"] == "trade", f"S2 should trade NO: {r['reasoning']}"
        assert r["side"] == "no"
        assert r["signals"]["market_edge"] >= 0.04


class TestS2FvSkips:
    """S2 must skip when the dislocation is absent, implausible, or the book is bad."""

    def test_skips_low_z(self):
        """Spot barely past strike -> |z| below the conviction gate -> skip."""
        r = _run_s2("SOL", spot=150.02, strike=150.0, yes_ask=40.0, no_ask=58.0)
        assert r["action"] == "skip"
        assert "s2_fv_lowz" in r["reasoning"]

    def test_skips_when_mid_already_fair(self):
        """Spot above strike but the mid already prices it in -> no edge -> EV-gate skip."""
        # Fair ~0.816, mid 0.795 -> gap ~0.02 below the 0.04 market-edge floor.
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=80.0, no_ask=21.0)
        assert r["action"] == "skip"
        assert "s2_ev_gate" in r["reasoning"], f"expected ev gate: {r['reasoning']}"

    def test_skips_tgtbt_extreme_gap(self):
        """Fair ~0.816 vs mid 0.41 (gap ~0.40) -> reject the trade, don't clamp it."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=40.0, no_ask=58.0)
        assert r["action"] == "skip"
        assert "s2_tgtbt" in r["reasoning"], f"expected tgtbt gate: {r['reasoning']}"
        assert r["signals"]["gap"] > 0.15

    def test_skips_tail_ban(self):
        """The traded side's de-vigged mid under 20c -> tail ban (longshots stay banned)."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=21.0, no_ask=82.0)
        assert r["action"] == "skip"
        assert "s2_tail_ban" in r["reasoning"], f"expected tail ban: {r['reasoning']}"

    def test_skips_fresh_book(self):
        """A book that already repriced (mid moved >= 3c over the window) is not stale."""
        ticker = "KXSOL-FRESH"
        now = time.time()
        hist = collections.deque(maxlen=120)
        hist.append((now - 60.0, 66.0))
        hist.append((now - 30.0, 69.0))
        hist.append((now, 72.0))          # mid moved 6c over the window
        bot_state._contract_mid_history[ticker] = hist
        _seed_spot("SOL", 150.44, True, 150.0)
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            r = strategy_brain_s2(150.44, 150.0, 73.0, 29.0, 420.0, 480.0, ticker, asset="SOL")
        assert r["action"] == "skip"
        assert "s2_fresh_book" in r["reasoning"], f"expected fresh-book skip: {r['reasoning']}"
        assert r["signals"]["mid_hist_n"] == 3

    def test_skips_wide_spread(self):
        """A wide round-trip spread (high vig) must skip even with a real dislocation."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=40.0, no_ask=68.0)
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
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            r = strategy_brain_s2(150.6, 150.0, 40.0, 58.0, 420.0, 480.0, "KXSOL-FLICK", asset="SOL")
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
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=40.0, no_ask=58.0, secs_left=60.0)
        assert r["action"] == "skip"
        assert "s2_time_gate" in r["reasoning"]

    def test_time_gate_early_window(self):
        """Entries with more than 9 minutes left are blocked (books widest early)."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=73.0, no_ask=29.0, secs_left=600.0)
        assert r["action"] == "skip"
        assert "s2_time_gate" in r["reasoning"]


def test_s2_config_present_for_enabled_assets():
    """The S2 fair-value config must cover BTC/SOL/XRP/DOGE."""
    for a in ("BTC", "SOL", "XRP", "DOGE"):
        assert a in _S2_FV_CONFIG
        assert _S2_FV_CONFIG[a]["min_z"] > 0
