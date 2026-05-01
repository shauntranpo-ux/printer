"""
Regression snapshot: proves BaseStrategy uses compute_15m_signal (D3-hybrid) for all markets.
"""
import collections
import time
from unittest.mock import patch as _patch

import pytest

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


class _Strat(BaseStrategy):
    pass


_SKIP_CFG = SkipConfig()


def _make_features(n_prices: int = 60, current_price: float = 100_000.0,
                   strike: float = 99_500.0, rv: float = 0.002,
                   is_rising: bool = True) -> MarketFeatures:
    dq = collections.deque(maxlen=3600)
    ts = time.time()
    for i in range(n_prices):
        price = current_price + (i * 10.0 if is_rising else -i * 10.0)
        dq.append((ts - (n_prices - i) * 60.0, price))
    return MarketFeatures(
        asset="BTC",
        ticker="BTC-TEST",
        timestamp=ts,
        current_price=current_price,
        strike=strike,
        btc_price=current_price,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=65.0,
        no_ask=37.0,
        yes_bid=64.0,
        no_bid=36.0,
        spread_yes=1.0,
        spread_no=1.0,
        prices_60m=dq,
        realized_vol_1min=rv,
    )


# ── 15m path: new D3-hybrid ────────────────────────────────────────────────────

def test_15m_path_uses_d3_hybrid():
    """is_15m=True routes through compute_15m_signal."""
    strat = _Strat(
        asset="BTC", skip_config=_SKIP_CFG,
        min_ev=0.001, stake_dollars=50.0,
    )
    features = _make_features(n_prices=60, is_rising=True)
    with _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=("yes", 0.72),
    ):
        d = strat.decide(features)
    assert d.contributing_signals.get("signal_name") == "d3_hybrid"


def test_15m_none_signal_skips():
    """compute_15m_signal returning None → skip."""
    strat = _Strat(
        asset="BTC", skip_config=_SKIP_CFG,
        min_ev=0.05, stake_dollars=50.0,
    )
    features = _make_features(n_prices=60)
    with _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=None,
    ):
        d = strat.decide(features)
    assert d.action == "skip"
    assert "fifteen_min_signal_insufficient_data" in d.reason


def test_15m_calibrated_p_in_signals():
    """calibrated_p_yes and raw_p_yes both appear in contributing_signals."""
    strat = _Strat(
        asset="BTC", skip_config=_SKIP_CFG,
        min_ev=0.001, stake_dollars=50.0,
    )
    features = _make_features(n_prices=60, is_rising=True)
    with _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=("yes", 0.65),
    ):
        d = strat.decide(features)

    if d.action == "trade":
        sigs = d.contributing_signals
        assert "raw_p_yes" in sigs
        assert "calibrated_p_yes" in sigs
        assert sigs["raw_p_yes"] == pytest.approx(0.65)


def test_15m_no_side_signal():
    """A 'no' side signal produces a no-side decision or skip (not a yes trade)."""
    strat = _Strat(
        asset="BTC", skip_config=_SKIP_CFG,
        min_ev=0.001, stake_dollars=50.0,
    )
    features = _make_features(n_prices=60, is_rising=False,
                               current_price=99_000.0, strike=99_500.0)
    with _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=("no", 0.35),
    ):
        d = strat.decide(features)
    assert d.side != "yes" or d.action == "skip"
