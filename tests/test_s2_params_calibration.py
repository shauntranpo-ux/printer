"""Tests that live S2 strategy params match calibration script constants.
Blocks param drift between bot_strategy.py and calibrate_winrates.py.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _S2_ASSET_CONFIG

# Calibration constants from scripts/calibrate_winrates.py lines 66-72
_CAL_S2 = {
    "BTC":  dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4),
    "ETH":  dict(min_dist=0.0030, min_vel_delta=0.70, vel_lookback=4),
    "SOL":  dict(min_dist=0.0060, min_vel_delta=1.20, vel_lookback=3),
    "XRP":  dict(min_dist=0.0050, min_vel_delta=0.90, vel_lookback=4),
    "DOGE": dict(min_dist=0.0100, min_vel_delta=1.50, vel_lookback=3),
}

def test_s2_min_dist_matches_calibration():
    """S2 min_dist must match calibration per-asset — win rate tables calibrated on these."""
    for asset, cal in _CAL_S2.items():
        live = _S2_ASSET_CONFIG[asset]["min_dist"]
        assert live == cal["min_dist"], (
            f"S2 {asset} min_dist mismatch: live={live} calibration={cal['min_dist']}. "
            "Win rate tables only cover trades >= calibration's min_dist threshold."
        )

def test_s2_time_min_in_calibrated_range():
    """S2 time_min must be >= 2.0 (calibration's earliest entry offset)."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["time_min"] >= 2.0, (
            f"S2 {asset} time_min={cfg['time_min']} < 2.0. Calibration only covers "
            "entries at >=2 min remaining -- earlier entries use tanh fallback with wrong WR."
        )

def test_s2_time_max_in_calibrated_range():
    """S2 time_max must be <= 12.5 (calibration's latest entry offset + small buffer)."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["time_max"] <= 12.5, (
            f"S2 {asset} time_max={cfg['time_max']} > 12.5. Calibration only covers "
            "entries at <=12 min remaining."
        )

def test_s2_min_ev_above_marginal_threshold():
    """S2 min_ev must be > 0 to filter out borderline trades."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["min_ev"] >= 0.01, (
            f"S2 {asset} min_ev={cfg['min_ev']} < 0.01. Marginal trades with barely-positive EV "
            "get filtered by fees and slippage in practice."
        )

def test_s1_min_ev_above_marginal_threshold():
    """S1 min_ev must be > 0 to filter out borderline trades."""
    from bot_strategy import _S1_ASSET_CONFIG
    for asset, cfg in _S1_ASSET_CONFIG.items():
        assert cfg["min_ev"] >= 0.01, (
            f"S1 {asset} min_ev={cfg['min_ev']} < 0.05."
        )
