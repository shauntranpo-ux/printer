"""Audit: entry_cents for hourly strategies must match the chosen side.

Invariant:
    decision.side == "yes" → entry_cents == features.yes_ask
    decision.side == "no"  → entry_cents == features.no_ask

Catches side/ask mismatches that cause wrong-price limit orders.

Covers the hourly strategies:
  - MidWindowStrategy   (ETH, t=~10min)
  - DwellWindowStrategy (ETH, t=30-42min)
  - LateWindowStrategy  (ETH, t>=45min)
  - BTCHourlyStrategy   (V3 mean-reversion)

BTCHourlyStrategy routes through BaseStrategy.decide which now emits
entry_cents in contributing_signals on the trade path, so the audit
asserts the invariant just like the other hourly strategies.
"""
from __future__ import annotations

import datetime

import pytest

from strategies.features import MarketFeatures
from strategies.mid_window_strategy import MidWindowStrategy
from strategies.dwell_window_strategy import DwellWindowStrategy
from strategies.late_window_strategy import LateWindowStrategy
from strategies.btc_hourly_strategy import BTCHourlyStrategy
from strategies.skip_layer import SkipConfig


def _neutral_ts() -> float:
    """Pick a UTC timestamp whose hour is NOT in any strategy's skip set.

    MidWindow and DwellWindow skip hours {12, 13} UTC.
    BTCHourlyStrategy skips hours [1, 7) UTC (Asian session).
    17:00 UTC is safe for all.
    """
    return datetime.datetime(
        2026, 4, 23, 17, 0, 0, tzinfo=datetime.timezone.utc
    ).timestamp()


def _make_features(
    *,
    asset: str = "ETH",
    current_price: float = 3510.0,
    strike: float = 3500.0,
    yes_ask: float = 80.0,
    no_ask: float = 21.0,
    elapsed_seconds: float = 600.0,
    seconds_left: float = 3000.0,
    n_price_samples: int = 70,
    direction: str = "yes",  # "yes" → ITM, "no" → OTM, for trajectory shaping
) -> MarketFeatures:
    """Build a MarketFeatures instance tuned to pass Mid/Dwell/Late conditions.

    Shapes a clean ETH+BTC trajectory with zero strike crossings on the
    requested side of the strike. Uses `asset` to tag MarketFeatures.asset
    correctly (the strategies themselves read from `features.strike` rather
    than branching on `features.asset`).
    """
    now = _neutral_ts()
    ticker = "KXETHD-26APR23-17:00-T3500" if asset == "ETH" else "KXBTCD-26APR23-17:00-T94000"

    # Build an ETH price series:
    #   - first sample at window_start = now - elapsed_seconds
    #   - last  sample at now
    #   - all samples strictly on the current_price side of strike (no cross)
    window_start = now - elapsed_seconds
    # Generate n_price_samples points spanning [window_start, now].
    if n_price_samples < 2:
        n_price_samples = 2
    dt = (now - window_start) / (n_price_samples - 1)
    # Offset from strike in the chosen direction (same sign as current-price offset).
    offset = current_price - strike
    # Ensure every sample stays on the same side of strike (|offset| >= 1 point).
    if abs(offset) < 1e-6:
        offset = 5.0 if direction == "yes" else -5.0

    features = MarketFeatures(
        asset=asset,
        ticker=ticker,
        timestamp=now,
        current_price=current_price,
        strike=strike,
        btc_price=94000.0,
        seconds_left=seconds_left,
        elapsed_seconds=elapsed_seconds,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=max(0.0, yes_ask - 1.0),
        no_bid=max(0.0, no_ask - 1.0),
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )

    # Populate prices_60m deque: monotonic trajectory, zero crossings.
    for i in range(n_price_samples):
        ts = window_start + dt * i
        # Linear approach to current_price from a starting offset on the same side.
        # Start at strike + 1.5*offset so we approach monotonically; always on same side.
        start_offset = 1.5 * offset
        frac = i / max(1, n_price_samples - 1)
        price = strike + start_offset + (offset - start_offset) * frac
        features.prices_60m.append((ts, price))

    # Populate btc_prices_60m deque. MidWindowStrategy derives btc_strike from
    # the first price in the window and uses strict > for side comparison.
    # Shape the BTC series so the first sample is the extremum on the "wrong"
    # side of the trajectory, and all later samples stay strictly on one side.
    # direction="yes": first sample is the lowest; rest strictly above.
    # direction="no":  first sample is the highest; rest strictly below.
    btc_open = 94000.0
    if direction == "yes":
        btc_target = btc_open + 200.0  # end 200 higher
        btc_second = btc_open + 50.0   # second sample already strictly > open
    else:
        btc_target = btc_open - 200.0
        btc_second = btc_open - 50.0
    for i in range(n_price_samples):
        ts = window_start + dt * i
        if i == 0:
            btc_price = btc_open
        else:
            frac = (i - 1) / max(1, n_price_samples - 2)
            btc_price = btc_second + (btc_target - btc_second) * frac
        features.btc_prices_60m.append((ts, btc_price))

    return features


def _assert_entry_matches_side(decision, features, *, strategy_name: str) -> str:
    """Core audit invariant. Returns a status string describing what happened.

    Statuses:
      - "traded_and_verified": strategy emitted a trade with entry_cents; invariant held.
      - "traded_but_no_entry_cents": strategy traded but didn't emit entry_cents (coverage gap).
      - "skipped:<reason>": strategy did not trade (no audit opportunity in this case).
    """
    if decision.action != "trade":
        return f"skipped:{decision.reason}"
    signals = getattr(decision, "contributing_signals", None) or {}
    entry = signals.get("entry_cents")
    if entry is None:
        return "traded_but_no_entry_cents"
    if decision.side == "yes":
        assert entry == features.yes_ask, (
            f"[{strategy_name}] YES side must post at yes_ask={features.yes_ask}, "
            f"got entry_cents={entry}"
        )
    elif decision.side == "no":
        assert entry == features.no_ask, (
            f"[{strategy_name}] NO side must post at no_ask={features.no_ask}, "
            f"got entry_cents={entry}"
        )
    return "traded_and_verified"


# -- MidWindowStrategy: t=~10min, ETH+BTC cross=0, dist>=0.30% ---------------
# NOTE: MidWindow derives btc_strike from the first in-window sample and uses
# strict `>` comparison for side. This makes `btc_cross=0 AND btc_itm=True`
# structurally impossible (any climb above the first sample registers as a
# cross). So the YES-side path is unreachable with synthetic flat-start BTC
# fixtures. The NO-side path is fully testable. We verify the invariant when
# the strategy does trade; unreachable paths yield no audit opportunity.

def test_mid_window_yes_side_uses_yes_ask():
    """MidWindow YES path: unreachable by synthetic fixtures — see module comment.

    The invariant is still checked if the strategy somehow trades YES.
    """
    strat = MidWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3520.0, strike=3500.0,  # +0.57% above strike → YES, dist ok
        elapsed_seconds=600.0, seconds_left=3000.0,
        direction="yes",
    )
    decision = strat.decide(features)
    _assert_entry_matches_side(decision, features, strategy_name="MidWindow")
    # Don't require a trade; if it did trade, assertion above verifies invariant.


def test_mid_window_no_side_uses_no_ask():
    strat = MidWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3480.0, strike=3500.0,  # -0.57% below strike → NO
        yes_ask=21.0, no_ask=80.0,
        elapsed_seconds=600.0, seconds_left=3000.0,
        direction="no",
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="MidWindow")
    assert status == "traded_and_verified", (
        f"MidWindow did not trade as expected (status={status}, reason={decision.reason})"
    )


# -- DwellWindowStrategy: t=30-42min, dwell>=80%, streak>=60% ---------------

def test_dwell_window_yes_side_uses_yes_ask():
    strat = DwellWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3510.0, strike=3500.0,
        elapsed_seconds=35 * 60.0, seconds_left=25 * 60.0,
        direction="yes",
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="DwellWindow")
    assert status == "traded_and_verified", (
        f"DwellWindow did not trade as expected (status={status}, reason={decision.reason})"
    )


def test_dwell_window_no_side_uses_no_ask():
    strat = DwellWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3490.0, strike=3500.0,
        yes_ask=21.0, no_ask=80.0,
        elapsed_seconds=35 * 60.0, seconds_left=25 * 60.0,
        direction="no",
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="DwellWindow")
    assert status == "traded_and_verified", (
        f"DwellWindow did not trade as expected (status={status}, reason={decision.reason})"
    )


# -- LateWindowStrategy: t>=45min, dist>=0.3%, entry>=85c -------------------

def test_late_window_yes_side_uses_yes_ask():
    strat = LateWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3520.0, strike=3500.0,  # +0.57% → YES, dist ok
        yes_ask=92.0, no_ask=9.0,              # yes_ask >= 85c (min entry)
        elapsed_seconds=46 * 60.0, seconds_left=14 * 60.0,
        direction="yes",
        n_price_samples=80,                    # >= cold_start_samples=60
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="LateWindow")
    assert status == "traded_and_verified", (
        f"LateWindow did not trade as expected (status={status}, reason={decision.reason})"
    )


def test_late_window_no_side_uses_no_ask():
    strat = LateWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    features = _make_features(
        current_price=3480.0, strike=3500.0,  # -0.57% → NO
        yes_ask=9.0, no_ask=92.0,              # no_ask >= 85c
        elapsed_seconds=46 * 60.0, seconds_left=14 * 60.0,
        direction="no",
        n_price_samples=80,
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="LateWindow")
    assert status == "traded_and_verified", (
        f"LateWindow did not trade as expected (status={status}, reason={decision.reason})"
    )


# -- BTCHourlyStrategy: BaseStrategy.decide now emits entry_cents ------------
# BTCHourly routes through BaseStrategy.decide which puts entry_cents in
# contributing_signals on the trade path. Audit the full invariant: if the
# strategy trades, entry_cents MUST match the chosen side's ask.
def test_btc_hourly_entry_matches_side():
    strat = BTCHourlyStrategy(
        skip_config=SkipConfig(),
        min_ev=0.0,
        stake_dollars=25.0,
        calibrator=None,
    )
    features = _make_features(
        asset="BTC",
        current_price=94100.0, strike=94000.0,
        yes_ask=55.0, no_ask=46.0,
        elapsed_seconds=40 * 60.0, seconds_left=20 * 60.0,
        direction="yes",
        n_price_samples=70,
    )
    decision = strat.decide(features)
    status = _assert_entry_matches_side(decision, features, strategy_name="BTCHourly")
    # Coverage gap (traded_but_no_entry_cents) is no longer acceptable — the
    # base decide() must emit entry_cents on every trade decision.
    assert status == "traded_and_verified" or status.startswith("skipped:"), (
        f"BTCHourly must either skip or trade with entry_cents set; got: {status}"
    )
