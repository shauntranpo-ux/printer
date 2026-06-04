"""
End-to-end tests: strategy_brain_s2 fires on realistic AMM market conditions.

These tests prove the full signal pipeline — velocity accumulation, OBI handling,
win rate lookup, EV gate — without mocking the brain logic itself.
"""
import time
import collections
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
from bot_strategy import strategy_brain_s2, _S2_ASSET_CONFIG


@pytest.fixture(autouse=True)
def _clean_state():
    """Wipe velocity history and OBI state between tests."""
    bot_state._contract_price_history.clear()
    bot_state._ticker_obi.clear()
    yield
    bot_state._contract_price_history.clear()
    bot_state._ticker_obi.clear()


def _seed_velocity(ticker: str, asset: str, direction: str = "yes"):
    """
    Seed contract price history with a strong velocity signal (3x minimum).
    Conviction gate requires 1.5x min_vel_delta; 3x gives safety margin.
    delta ~= 2*step, so step = 1.5*min_vel gives delta = 3*min_vel.
    """
    cfg = _S2_ASSET_CONFIG[asset]
    lookback = cfg["vel_lookback"]
    min_vel  = cfg["min_vel_delta"]
    step = (min_vel * 3.0) / 2.0
    base = 70.0
    n = lookback + 1
    history = collections.deque(maxlen=60)
    if direction == "yes":
        prices = [base + i * step for i in range(n)]
    else:
        prices = [base + (n - 1 - i) * step for i in range(n)]
    now = time.time()
    for i, p in enumerate(prices):
        history.append((now - (n - 1 - i) * 10, p))
    bot_state._contract_price_history[ticker] = history


class TestS2FiresETH:
    """S2 fires for ETH in the market-uncertainty zone (entry <= 50c)."""

    def test_s2_fires_amm_obi_none(self):
        """S2 fires when OBI=None (AMM market), velocity strong, entry <=50c."""
        ticker = "KXETH-25MAY30-T2800"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "trade", (
            f"S2 should trade but got: {result['reasoning']}\n"
            "Likely OBI gate still fails closed — check _s2_obi_gate None branch."
        )
        assert result["side"] == "yes"
        assert result["win_prob"] >= 0.54, f"win_prob {result['win_prob']:.3f} below realistic floor"

    def test_s2_fires_with_explicit_none_obi(self):
        """S2 fires when _ticker_obi[ticker] is explicitly set to None (AMM fetch path)."""
        ticker = "KXETH-25MAY30-T2800B"
        _seed_velocity(ticker, "ETH", direction="yes")
        bot_state._ticker_obi[ticker] = None
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "trade", (
            f"S2 should trade with explicit None OBI: {result['reasoning']}"
        )

    def test_s2_skips_when_velocity_below_threshold(self):
        """S2 skips when velocity delta < min_vel_delta."""
        ticker = "KXETH-25MAY30-T2800C"
        history = collections.deque(maxlen=60)
        now = time.time()
        for i in range(6):
            history.append((now - (5 - i) * 10, 70.0 + i * 0.05))
        bot_state._contract_price_history[ticker] = history
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_vel_flat" in result["reasoning"] or "s2_no_velocity_data" in result["reasoning"]

    def test_s2_skips_expensive_entry(self):
        """S2 skips when entry > 50c cap — market already priced the move in."""
        ticker = "KXETH-25MAY30-T2800E"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=75.0,
            no_ask=25.0,
            elapsed_seconds=150.0,
            secs_left=600.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert (
            "s2_price_filter" in result["reasoning"]
            or "s2_ev_gate" in result["reasoning"]
            or "s2_vel_flat" in result["reasoning"]
        )

    def test_s2_skips_weak_velocity_below_1x5(self):
        """S2 must skip when velocity < 1.5x min_vel_delta (conviction gate)."""
        ticker = "KXETH-25MAY30-T2800F"
        cfg = _S2_ASSET_CONFIG["ETH"]
        # Seed velocity at exactly 1.0x threshold — passes detection, fails 1.5x conviction
        lookback = cfg["vel_lookback"]
        min_vel  = cfg["min_vel_delta"]
        step = (min_vel * 1.0) / 2.0  # gives vel_delta ~= 1.0x min_vel
        base = 70.0
        history = collections.deque(maxlen=60)
        now = time.time()
        prices = [base + i * step for i in range(lookback + 1)]
        for i, p in enumerate(prices):
            history.append((now - (lookback - i) * 10, p))
        bot_state._contract_price_history[ticker] = history

        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=480.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_vel_weak" in result["reasoning"], \
            f"Expected conviction skip, got: {result['reasoning']}"


    def test_s2_skips_reversal_velocity_yes_but_price_below_strike(self):
        """S2 must reject: velocity=YES but asset price < strike (reversal — no edge)."""
        ticker = "KXETH-25MAY30-T2900G"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,   # price BELOW strike
            strike=2900.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip", \
            f"S2 should reject reversal but got: {result['reasoning']}"
        assert "s2_reversal_gate" in result["reasoning"], \
            f"Expected s2_reversal_gate, got: {result['reasoning']}"

    def test_s2_skips_reversal_velocity_no_but_price_above_strike(self):
        """S2 must reject: velocity=NO but asset price > strike (reversal — no edge)."""
        ticker = "KXETH-25MAY30-T2700H"
        _seed_velocity(ticker, "ETH", direction="no")
        result = strategy_brain_s2(
            btc_price=2850.0,   # price ABOVE strike
            strike=2700.0,
            yes_ask=70.0,
            no_ask=30.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip", \
            f"S2 should reject reversal but got: {result['reasoning']}"
        assert "s2_reversal_gate" in result["reasoning"], \
            f"Expected s2_reversal_gate, got: {result['reasoning']}"


class TestS2FiresMultiAsset:
    """S2 fires for all enabled assets at uncertainty-zone entries."""

    @pytest.mark.parametrize("asset,strike,price,yes_ask", [
        ("ETH",  2800.0, 2850.0, 45.0),
        ("SOL",  145.0,  148.0,  45.0),
        ("XRP",  2.30,   2.35,   45.0),
    ])
    def test_s2_fires_per_asset(self, asset, strike, price, yes_ask):
        """Each enabled asset fires with strong velocity and <=50c entry."""
        ticker = f"KXMOCK-{asset}-T{int(strike)}"
        _seed_velocity(ticker, asset, direction="yes")
        result = strategy_brain_s2(
            btc_price=price,
            strike=strike,
            yes_ask=yes_ask,
            no_ask=100 - yes_ask,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset=asset,
        )
        assert result["action"] == "trade", (
            f"S2 failed to fire for {asset}: {result['reasoning']}"
        )
