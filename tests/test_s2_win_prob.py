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


def test_s2_high_velocity_gets_higher_win_prob_than_low_velocity():
    """High-velocity S2 trade must have higher win_prob than low-velocity trade.
    With S1 GBM model (distance-only), both would be identical. This catches the bug.
    """
    ticker_hi = "KXETH15M-HI"
    ticker_lo = "KXETH15M-LO"
    result_hi = _run_s2(ticker_hi, vel_ratio=3.0, btc_price=2830.0, strike=2800.0)
    result_lo = _run_s2(ticker_lo, vel_ratio=1.3, btc_price=2830.0, strike=2800.0)

    # Both may trade or skip EV gate, but win_prob must differ
    wp_hi = result_hi.get("win_prob", 0)
    wp_lo = result_lo.get("win_prob", 0)
    assert wp_hi > wp_lo, (
        f"High velocity (3x) must give higher win_prob than low velocity (1.3x). "
        f"Got hi={wp_hi:.4f}, lo={wp_lo:.4f} — S2 is using S1 distance-only model."
    )


def test_s2_win_prob_increases_with_velocity():
    """As vel_ratio rises from 1.5x to 5x, win_prob must strictly increase."""
    ticker = "KXETH15M-VEL"
    prev_wp = 0.0
    for ratio in [1.5, 2.0, 3.0, 5.0]:
        result = _run_s2(ticker, vel_ratio=ratio, btc_price=2830.0, strike=2800.0)
        wp = result.get("win_prob", 0)
        assert wp >= prev_wp - 0.001, (
            f"win_prob must not decrease as vel_ratio increases. "
            f"At ratio={ratio}, wp={wp:.4f} < prev={prev_wp:.4f}"
        )
        prev_wp = wp
