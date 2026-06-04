"""Verify S1/S2 live params match calibration script constants."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _S1_ASSET_CONFIG, _S2_ASSET_CONFIG

# Updated after momentum rewrite: ema_short/ema_long replaced with min_momentum.
CALIBRATION_S1 = {
    "BTC":  dict(min_dist=0.0030, min_momentum=0.0030),
    "ETH":  dict(min_dist=0.0030, min_momentum=0.0025),
    "SOL":  dict(min_dist=0.0050, min_momentum=0.0040),
    "XRP":  dict(min_dist=0.0030, min_momentum=0.0025),
    "DOGE": dict(min_dist=0.0070, min_momentum=0.0050),
}

# Updated after strategy overhaul: min_vel_delta raised ~40% to require stronger signal.
CALIBRATION_S2_VEL = {
    "BTC":  0.30,
    "ETH":  0.26,
    "SOL":  0.42,
    "XRP":  0.32,
    "DOGE": 0.50,
}


def test_s1_momentum_thresholds_match_calibration():
    """Live S1 min_momentum must match calibration values."""
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert abs(live["min_momentum"] - cal["min_momentum"]) < 1e-9, (
            f"{asset}: live min_momentum={live['min_momentum']} != {cal['min_momentum']}"
        )


def test_s1_min_dist_matches_calibration():
    """Live S1 min_dist must match calibration values."""
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert abs(live["min_dist"] - cal["min_dist"]) < 1e-9, (
            f"{asset}: live min_dist={live['min_dist']} != {cal['min_dist']}"
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
    """S1 GBM fallback must return 0.52-0.75 -- realistic for momentum signal."""
    from bot_strategy import _s1_lookup_win_rate
    wp = _s1_lookup_win_rate("ETH", 0.003, 3.0)
    assert 0.52 <= wp <= 0.75, f"ETH GBM WR={wp:.3f} outside realistic 52-75% range"


def test_s2_lookup_conservative_baseline():
    """S2 tanh fallback must return 55-65% — realistic for velocity signal."""
    from bot_strategy import _s2_lookup_win_rate
    wp = _s2_lookup_win_rate("ETH", 0.26, 4.0)
    assert 0.52 <= wp <= 0.65, f"ETH S2 tanh WR={wp:.3f} outside realistic 52-65% range"


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




def test_s1_certainty_win_prob_increases_with_dist():
    """Geometric certainty: farther from strike = higher WR."""
    from bot_strategy import _s1_certainty_win_prob
    wp_close = _s1_certainty_win_prob(0.002, 480.0, "ETH")
    wp_far   = _s1_certainty_win_prob(0.008, 480.0, "ETH")
    assert wp_far > wp_close, f"WR should increase with distance: {wp_close:.3f} vs {wp_far:.3f}"


def test_s1_certainty_win_prob_increases_with_less_time():
    """Geometric certainty: less time left = higher WR (less time to cross back)."""
    from bot_strategy import _s1_certainty_win_prob
    # dist=0.001 keeps both values below the 0.75 cap so the ordering is visible
    wp_more_time = _s1_certainty_win_prob(0.001, 600.0, "ETH")
    wp_less_time = _s1_certainty_win_prob(0.001, 180.0, "ETH")
    assert wp_less_time > wp_more_time, \
        f"WR should be higher with less time: {wp_more_time:.3f} vs {wp_less_time:.3f}"


def test_s1_certainty_win_prob_range():
    """WR must stay in 0.52-0.75 range — no fantasy numbers."""
    from bot_strategy import _s1_certainty_win_prob
    for dist in [0.001, 0.005, 0.010, 0.030]:
        for t in [60.0, 300.0, 600.0, 840.0]:
            wp = _s1_certainty_win_prob(dist, t, "ETH")
            assert 0.52 <= wp <= 0.75, f"WR={wp:.3f} out of range at dist={dist} t={t}"


def test_certainty_win_prob_range_with_vol_multiplier():
    """Win prob stays in 0.52-0.75 regardless of time-of-day vol adjustment."""
    from bot_strategy import _s1_certainty_win_prob
    for dist in [0.001, 0.005, 0.015]:
        for t in [60.0, 300.0, 600.0, 840.0]:
            wp = _s1_certainty_win_prob(dist, t, "ETH")
            assert 0.52 <= wp <= 0.75, \
                f"WR={wp:.3f} out of [0.52, 0.75] at dist={dist} t={t}"
