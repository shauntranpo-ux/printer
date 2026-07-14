"""
End-to-end tests: strategy_brain_s2 (FAVORITE-BIAS HARVEST) fires and skips correctly.

S2 fires LATE (2.5-6 min left) when the spot sits decisively past the strike (|z| large)
and the FAVORITE side's de-vigged mid is in the premium band (0.70-0.88). It buys that
favorite. There is no fair-value-disagreement gate; a light guard only skips favorites the
model strongly rejects. Fixtures pin the ToD multiplier to 1.0 and use secs_left=240
(4 min, inside the late window). At 4 min the static-vol z for the seeded spots is ~1.3.
"""
import sys, os, time, collections
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s2, _S2_FAV_CONFIG


@pytest.fixture(autouse=True)
def _restore_prices():
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
    """Seed ~78s of prints all on one side of the strike, ending at last_price - the
    spot-sign confirmation gate needs the last prints on the favorite side."""
    now = time.time()
    dq = collections.deque(maxlen=2000)
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


def _run_s2(asset, spot, strike, yes_ask, no_ask, secs_left=240.0, cfg_extra=None):
    config = {"mode": "paper", "quiet_hours_enabled": False, "calibration_enabled": False,
              "auto_gate_enabled": False}
    if cfg_extra:
        config.update(cfg_extra)
    above = spot >= strike
    _seed_spot(asset, spot, above, strike)
    ticker = f"KXMOCK-{asset}-T{int(strike) if strike >= 1 else strike}"
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        return strategy_brain_s2(
            btc_price=spot, strike=strike, yes_ask=yes_ask, no_ask=no_ask,
            elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
            ticker=ticker, asset=asset,
        )


class TestS2FavoriteFires:
    """S2 buys the proven favorite when its mid is in the premium band."""

    @pytest.mark.parametrize("asset,strike,spot", [
        ("BTC", 60000.0, 60088.0),
        ("SOL", 150.0, 150.44),
        ("XRP", 2.30, 2.3063),
        ("DOGE", 0.150, 0.15048),
    ])
    def test_fires_yes_favorite(self, asset, strike, spot):
        """Spot decisively above strike + favorite YES mid ~0.77 -> buy YES."""
        r = _run_s2(asset, spot, strike, yes_ask=78.0, no_ask=24.0)
        assert r["action"] == "trade", f"{asset} should buy the favorite: {r['reasoning']}"
        assert r["side"] == "yes"
        assert 0.70 <= r["signals"]["p_fav"] <= 0.88
        assert r["signals"]["z"] >= 0.8
        assert "model_raw_p_yes" in r["signals"]
        assert "sigma_eff" in r["signals"] and "spot" in r["signals"]

    def test_fires_no_favorite(self):
        """Spot decisively below strike -> NO is the favorite -> buy NO."""
        r = _run_s2("SOL", spot=149.56, strike=150.0, yes_ask=24.0, no_ask=78.0)
        assert r["action"] == "trade", f"S2 should buy the NO favorite: {r['reasoning']}"
        assert r["side"] == "no"
        assert 0.70 <= r["signals"]["p_fav"] <= 0.88


class TestS2FavoriteSkips:
    """S2 skips coin-flips, longshots, over-certain books, and wide/early markets."""

    def test_skips_low_z(self):
        """Spot barely past strike -> below the conviction gate -> skip."""
        r = _run_s2("SOL", spot=150.02, strike=150.0, yes_ask=78.0, no_ask=24.0)
        assert r["action"] == "skip"
        assert "s2_lowz" in r["reasoning"], r["reasoning"]

    def test_skips_not_favorite_coinflip(self):
        """High z but the favorite mid is only ~0.59 (a toss-up) -> not-favorite skip."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=60.0, no_ask=42.0)
        assert r["action"] == "skip"
        assert "s2_not_favorite" in r["reasoning"], r["reasoning"]

    def test_skips_too_certain(self):
        """Favorite mid ~0.91 (above the band) -> fee-eaten certainty -> skip."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=92.0, no_ask=10.0)
        assert r["action"] == "skip"
        assert "s2_too_certain" in r["reasoning"], r["reasoning"]

    def test_skips_wide_spread(self):
        """A wide round-trip spread (high vig) must skip even for a real favorite."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=78.0, no_ask=30.0)
        assert r["action"] == "skip"
        assert "s2_wide_spread" in r["reasoning"], r["reasoning"]

    def test_skips_spot_flicker(self):
        """Last prints straddling the strike -> confirmation gate skips."""
        now = time.time()
        dq = collections.deque(maxlen=2000)
        for i in range(40):
            p = 150.0 + (0.6 if i % 2 == 0 else -0.6)
            dq.append((now - (39 - i) * 2.0, p))
        asset_manager._prices["SOL"] = dq
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False,
                                 "calibration_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            r = strategy_brain_s2(150.6, 150.0, 78.0, 24.0, 660.0, 240.0, "KXSOL-FLICK", asset="SOL")
        assert r["action"] == "skip"
        assert "s2_flicker" in r["reasoning"] or "s2_lowz" in r["reasoning"], r["reasoning"]

    def test_eth_disabled_by_default(self):
        r = _run_s2("ETH", spot=3020.0, strike=3000.0, yes_ask=78.0, no_ask=24.0)
        assert r["action"] == "skip"
        assert "s2_fv_disabled" in r["reasoning"]

    def test_eth_enabled_via_config(self):
        r = _run_s2("ETH", spot=3020.0, strike=3000.0, yes_ask=78.0, no_ask=24.0,
                    cfg_extra={"s2_eth_enabled": True})
        assert r["reasoning"] != "s2_fv_disabled:ETH"

    def test_time_gate_too_early(self):
        """More than 6 min left -> favorite not yet proven -> skip."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=78.0, no_ask=24.0, secs_left=480.0)
        assert r["action"] == "skip"
        assert "s2_time_gate" in r["reasoning"]

    def test_time_gate_final_stretch(self):
        """Inside the final 90s auction tail -> skip."""
        r = _run_s2("SOL", spot=150.44, strike=150.0, yes_ask=78.0, no_ask=24.0, secs_left=60.0)
        assert r["action"] == "skip"
        assert "s2_time_gate" in r["reasoning"]


def test_s2_config_present_for_enabled_assets():
    for a in ("BTC", "SOL", "XRP", "DOGE"):
        assert a in _S2_FAV_CONFIG
        assert _S2_FAV_CONFIG[a]["min_z"] > 0
        assert 0.5 < _S2_FAV_CONFIG[a]["mid_lo"] < _S2_FAV_CONFIG[a]["mid_hi"] <= 0.95
