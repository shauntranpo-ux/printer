"""Verify S1/S2 live params match calibration script constants."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _S1_ASSET_CONFIG, _S2_ASSET_CONFIG

# Updated after strategy overhaul: min_dist raised to filter coin-flip trades.
CALIBRATION_S1 = {
    "BTC":  dict(min_dist=0.0030, ema_short=3, ema_long=10),
    "ETH":  dict(min_dist=0.0030, ema_short=3, ema_long=10),
    "SOL":  dict(min_dist=0.0050, ema_short=3, ema_long=8),
    "XRP":  dict(min_dist=0.0030, ema_short=3, ema_long=10),
    "DOGE": dict(min_dist=0.0070, ema_short=2, ema_long=8),
}

# Updated after strategy overhaul: min_vel_delta raised ~40% to require stronger signal.
CALIBRATION_S2_VEL = {
    "BTC":  0.30,
    "ETH":  0.26,
    "SOL":  0.42,
    "XRP":  0.32,
    "DOGE": 0.50,
}


def test_s1_ema_long_matches_calibration():
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert live["ema_long"] == cal["ema_long"], (
            f"{asset}: live ema_long={live['ema_long']} != calibration ema_long={cal['ema_long']}"
        )


def test_s1_ema_short_matches_calibration():
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert live["ema_short"] == cal["ema_short"], (
            f"{asset}: live ema_short={live['ema_short']} != calibration ema_short={cal['ema_short']}"
        )


def test_s1_min_dist_matches_calibration():
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert abs(live["min_dist"] - cal["min_dist"]) < 1e-9, (
            f"{asset}: live min_dist={live['min_dist']} != calibration min_dist={cal['min_dist']}"
        )


def test_s2_vel_delta_matches_calibration():
    for asset, cal_vel in CALIBRATION_S2_VEL.items():
        live = _S2_ASSET_CONFIG[asset]
        assert abs(live["min_vel_delta"] - cal_vel) < 1e-9, (
            f"{asset}: live min_vel_delta={live['min_vel_delta']} != calibration {cal_vel}"
        )


def test_s1_win_rate_tables_all_none():
    """All S1 WR table entries must be None — forces realistic tanh fallback."""
    from bot_strategy import _S1_WIN_RATE
    for asset, buckets in _S1_WIN_RATE.items():
        for key, val in buckets.items():
            assert val is None, (
                f"S1 WR {asset} bucket {key}={val} — must be None to use realistic tanh formula"
            )


def test_s2_win_rate_tables_all_none():
    """All S2 WR table entries must be None — forces realistic tanh fallback."""
    from bot_strategy import _S2_WIN_RATE
    for asset, buckets in _S2_WIN_RATE.items():
        for key, val in buckets.items():
            assert val is None, (
                f"S2 WR {asset} bucket {key}={val} — must be None to use realistic tanh formula"
            )


def test_s1_lookup_conservative_baseline():
    """S1 tanh fallback must return 55-65% — realistic for EMA momentum signal."""
    from bot_strategy import _s1_lookup_win_rate
    wp = _s1_lookup_win_rate("ETH", 0.003, 3.0)
    assert 0.54 <= wp <= 0.65, f"ETH tanh WR={wp:.3f} outside realistic 54-65% range"


def test_s2_lookup_conservative_baseline():
    """S2 tanh fallback must return 55-65% — realistic for velocity signal."""
    from bot_strategy import _s2_lookup_win_rate
    wp = _s2_lookup_win_rate("ETH", 0.26, 4.0)
    assert 0.54 <= wp <= 0.65, f"ETH S2 tanh WR={wp:.3f} outside realistic 54-65% range"


def test_s2_vel_flat_skips_below_threshold():
    """S2 must skip when velocity delta < min_vel_delta."""
    from collections import deque
    import bot_state
    from bot_strategy import strategy_brain_s2

    bot_state._contract_price_history["TEST-ETH"] = deque(
        [(0, 50.0), (1, 50.05), (2, 50.1), (3, 50.1), (4, 50.1)], maxlen=60
    )
    bot_state._ticker_obi["TEST-ETH"] = 0.5
    result = strategy_brain_s2(
        btc_price=2000, strike=2010, yes_ask=50, no_ask=51,
        elapsed_seconds=300, secs_left=600, ticker="TEST-ETH", asset="ETH",
    )
    assert result["action"] == "skip", f"Expected skip, got: {result['reasoning']}"
    assert "s2_vel" in result["reasoning"], f"Expected vel skip: {result['reasoning']}"
