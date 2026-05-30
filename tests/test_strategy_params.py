"""Verify S1/S2 live params match calibration script constants."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _S1_ASSET_CONFIG, _S2_ASSET_CONFIG

# Must match scripts/calibrate_winrates.py S1_ASSET_CONFIG exactly.
CALIBRATION_S1 = {
    "BTC":  dict(min_dist=0.0025, ema_short=3, ema_long=10),
    "ETH":  dict(min_dist=0.0030, ema_short=3, ema_long=10),
    "SOL":  dict(min_dist=0.0050, ema_short=3, ema_long=8),
    "XRP":  dict(min_dist=0.0040, ema_short=3, ema_long=10),
    "DOGE": dict(min_dist=0.0080, ema_short=2, ema_long=8),
}

# Must match scripts/calibrate_winrates.py S2_ASSET_CONFIG min_vel_delta exactly.
CALIBRATION_S2_VEL = {
    "BTC":  0.80,
    "ETH":  0.70,
    "SOL":  1.20,
    "XRP":  0.90,
    "DOGE": 1.50,
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


def test_s1_win_rate_eth_common_buckets_not_none():
    from bot_strategy import _S1_WIN_RATE
    for dist_idx in [0, 1]:
        for time_idx in [0, 1, 2]:
            val = _S1_WIN_RATE["ETH"].get((dist_idx, time_idx))
            assert val is not None, (
                f"ETH bucket ({dist_idx},{time_idx}) is None — insufficient calibration data"
            )


def test_s1_lookup_eth_above_breakeven():
    from bot_strategy import _s1_lookup_win_rate
    wp = _s1_lookup_win_rate("ETH", 0.003, 3.0)
    assert wp >= 0.85, f"ETH (0,0) bucket WR={wp:.3f} unexpectedly low — recalibration needed"
