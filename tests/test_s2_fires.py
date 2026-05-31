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
    Seed contract price history with a strong enough velocity signal.

    _s2_contract_direction compares first-half avg vs second-half avg of
    the last (lookback+1) ticks.  For n=lookback+1 ticks with a constant
    step between them:
        mid = n // 2
        first_avg  = base + (mid-1)/2 * step
        second_avg = base + (mid + (n-mid-1)/2) * step
        delta      = second_avg - first_avg  ≈  2.0 * step  (for all n>=4)

    So we need step >= min_vel_delta / 2.0.  Use 1.5x margin.
    """
    cfg = _S2_ASSET_CONFIG[asset]
    lookback = cfg["vel_lookback"]
    min_vel  = cfg["min_vel_delta"]
    # Factor of 2.0 derived from half-avg arithmetic above; 1.5x safety margin
    step = (min_vel * 1.5) / 2.0
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
    """S2 fires for ETH — 4-minute window, above-strike continuation."""

    def test_s2_fires_amm_obi_none(self):
        """S2 fires when OBI=None (AMM market), velocity strong enough."""
        ticker = "KXETH-25MAY30-T2800"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=72.0,
            no_ask=28.0,
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
        assert result["win_prob"] > 0.90

    def test_s2_fires_with_explicit_none_obi(self):
        """S2 fires when _ticker_obi[ticker] is explicitly set to None (AMM fetch path)."""
        ticker = "KXETH-25MAY30-T2800B"
        _seed_velocity(ticker, "ETH", direction="yes")
        bot_state._ticker_obi[ticker] = None
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=72.0,
            no_ask=28.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "trade", (
            f"S2 should trade with explicit None OBI: {result['reasoning']}"
        )

    def test_s2_skips_when_velocity_below_threshold(self):
        """S2 skips when velocity delta < min_vel_delta (not enough momentum)."""
        ticker = "KXETH-25MAY30-T2800C"
        history = collections.deque(maxlen=60)
        now = time.time()
        for i in range(6):
            history.append((now - (5 - i) * 10, 70.0 + i * 0.05))
        bot_state._contract_price_history[ticker] = history
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=72.0,
            no_ask=28.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_vel_flat" in result["reasoning"] or "s2_no_velocity_data" in result["reasoning"]

    def test_s2_skips_reversal_price_below_strike(self):
        """S2 velocity=yes but price below strike → reversal gate skips."""
        ticker = "KXETH-25MAY30-T2900D"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2900.0,
            yes_ask=38.0,
            no_ask=62.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_reversal_gate" in result["reasoning"]

    def test_s2_skips_when_ev_negative(self):
        """S2 skips when win_prob - entry/100 - fee < min_ev."""
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
        assert "s2_ev_gate" in result["reasoning"] or "s2_vel_flat" in result["reasoning"]


class TestS2FiresMultiAsset:
    """S2 fires for all enabled assets with their calibrated thresholds."""

    @pytest.mark.parametrize("asset,strike,price,yes_ask", [
        ("ETH",  2800.0, 2850.0, 72.0),
        ("SOL",  145.0,  148.0,  72.0),
        ("XRP",  2.30,   2.35,   72.0),
    ])
    def test_s2_fires_per_asset(self, asset, strike, price, yes_ask):
        """Each enabled asset's S2 fires with 4-min window and strong velocity."""
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
