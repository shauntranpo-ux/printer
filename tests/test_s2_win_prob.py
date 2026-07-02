"""S2 win probability must use velocity-aware _s2_lookup_win_rate, not S1 GBM."""
import sys, os, time, collections
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s2


def _contract_history(ticker: str, base: float, vel_ratio: float) -> None:
    """Populate _contract_price_history with a velocity signal at vel_ratio * min_vel.
    Uses vel_lookback=4 to match _s2_contract_direction's window.
    Creates 5 prices with strong trend over the recent window.
    """
    from bot_strategy import _S2_ASSET_CONFIG
    cfg = _S2_ASSET_CONFIG["ETH"]
    min_vel = cfg["min_vel_delta"]
    vel_lookback = cfg["vel_lookback"]  # = 4

    # Create enough history to fill the deque, then strong velocity in the last vel_lookback+1 ticks
    now = time.time()
    history = collections.deque(maxlen=60)

    # Fill with neutral history
    for i in range(55):
        history.append((now - (55 - i) * 10, base))

    # Last vel_lookback+1 ticks: strong velocity signal
    step = (min_vel * vel_ratio) / (vel_lookback // 2)  # spread across half the lookback window
    for i in range(vel_lookback + 1):
        history.append((now - (vel_lookback - i) * 10, base + i * step))

    bot_state._contract_price_history[ticker] = history


def _run_s2(ticker: str, vel_ratio: float, btc_price: float = 2800.0, strike: float = 2800.0,
            yes_ask: float = 40.0, no_ask: float = 55.0) -> dict:
    _contract_history(ticker, base=yes_ask, vel_ratio=vel_ratio)
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_ticker_obi", {ticker: 0.25}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s2(
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            no_ask=no_ask,
            elapsed_seconds=200.0,
            secs_left=400.0,
            ticker=ticker,
            asset="ETH",
        )


def _run_s2_fv(asset, spot, strike, secs_left=600.0):
    """Run the new spot_fv_disloc S2 with a seeded, sign-consistent spot deque."""
    from collections import deque
    now = time.time()
    above = spot >= strike
    if above:
        start = max(spot * 0.9995, strike * 1.001)
    else:
        start = min(spot * 1.0005, strike * 0.999)
    dq = deque([(now - (40 - i) * 2, start + (spot - start) * (i / 39.0)) for i in range(40)],
               maxlen=2000)
    saved = asset_manager._prices.get(asset)
    try:
        asset_manager._prices[asset] = dq
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False}):
            return strategy_brain_s2(
                btc_price=spot, strike=strike, yes_ask=40.0, no_ask=58.0,
                elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
                ticker=f"KX{asset}-FV", asset=asset,
            )
    finally:
        if saved is not None:
            asset_manager._prices[asset] = saved


def test_s2_raw_prob_increases_with_distance_from_strike():
    """
    The new S2 raw model prob (Bachelier fair value) must increase as the spot moves
    farther above the strike — direction/conviction come from the spot dislocation,
    NOT contract velocity.
    """
    near = _run_s2_fv("SOL", spot=150.4, strike=150.0)
    far = _run_s2_fv("SOL", spot=151.2, strike=150.0)
    wp_near = near.get("signals", {}).get("model_raw_p_yes", 0)
    wp_far = far.get("signals", {}).get("model_raw_p_yes", 0)
    assert wp_far > wp_near, (
        f"A spot farther above the strike must give a higher raw fair value. "
        f"Got near={wp_near:.4f}, far={wp_far:.4f}"
    )


def test_s2_tanh_ceiling_is_at_least_62_pct():
    """tanh ceiling (high velocity limit) must be at least 0.62 to match observed 55-62% WR."""
    from bot_strategy import _s2_lookup_win_rate
    # At high velocity, tanh saturates to ~1.0, so ceiling = 0.52 + 0.10 * 1.0 = 0.62
    high_vel = 10.0  # Arbitrary high velocity (well above any min_vel)
    wp = _s2_lookup_win_rate("ETH", vel_delta=high_vel, mins_left=60.0)
    assert wp >= 0.62, f"win_prob ceiling must be >= 0.62; got {wp:.4f}"


def test_s2_tanh_floor_is_0_52():
    """tanh floor (zero velocity) must be exactly 0.52."""
    from bot_strategy import _s2_lookup_win_rate
    # At zero velocity, tanh(0) = 0, so floor = 0.52 + 0.10 * 0 = 0.52
    wp = _s2_lookup_win_rate("ETH", vel_delta=0.0, mins_left=60.0)
    assert wp == 0.52, f"win_prob floor must be 0.52; got {wp:.4f}"
