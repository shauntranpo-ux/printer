"""
Run a strategy against a stream of BacktestEvents and collect per-trade
outcomes.

Each event represents one bot decision point. If the strategy returns
'trade', we simulate the fill at the orderbook ask on that side, hold
to window_close, settle against close_price vs strike.

Fills:
  - YES at yes_ask (round up fees per Kalshi formula)
  - NO at no_ask
  - Taker fees by default (conservative; maker-mode analyzed separately)
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from strategies.backtest.window_generator import BacktestEvent
from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.fees import taker_fee, maker_fee


@dataclass
class BacktestTrade:
    asset: str
    eval_ts: float
    window_start_ts: float
    window_close_ts: float
    side: str
    entry_price_cents: float
    raw_p_model: float
    calibrated_p_model: float
    contracts: int
    stake_dollars: float
    fee_dollars: float
    close_price: float
    strike: float
    outcome: str                 # win / loss
    payout_dollars: float
    pnl_dollars: float
    ev_at_entry: float
    reason: str
    signals_dump: dict


def event_to_features(event: BacktestEvent) -> MarketFeatures:
    """Convert a backtest event into a MarketFeatures instance."""
    prices_1m: deque = deque(maxlen=60)
    prices_5m: deque = deque(maxlen=300)
    prices_60m: deque = deque(maxlen=3600)

    for ts, p in event.price_history:
        prices_60m.append((ts, p))
        if ts >= event.eval_ts - 300:
            prices_5m.append((ts, p))
        if ts >= event.eval_ts - 60:
            prices_1m.append((ts, p))

    # Synthesize flat Kalshi history so velocity signal doesn't fire artificially.
    # Velocity contribution is analyzed via ablation in Task 10.6.
    kalshi_history: deque = deque(maxlen=60)
    for i in range(30):
        kalshi_history.append((event.eval_ts - (30 - i) * 10,
                               event.orderbook.yes_ask))

    return MarketFeatures(
        asset=event.asset,
        ticker=f"BT-{event.asset}",
        timestamp=event.eval_ts,
        current_price=event.current_price,
        strike=event.strike,
        btc_price=event.current_price if event.asset == "BTC" else 0.0,
        seconds_left=event.seconds_left,
        elapsed_seconds=event.elapsed_seconds,
        yes_ask=event.orderbook.yes_ask,
        no_ask=event.orderbook.no_ask,
        yes_bid=event.orderbook.yes_bid,
        no_bid=event.orderbook.no_bid,
        spread_yes=event.orderbook.yes_ask - event.orderbook.yes_bid,
        spread_no=event.orderbook.no_ask - event.orderbook.no_bid,
        prices_1m=prices_1m,
        prices_5m=prices_5m,
        prices_60m=prices_60m,
        kalshi_price_history=kalshi_history,
        realized_vol_1min=event.realized_vol_1min,
    )


def _settle_trade(
    side: str,
    entry_cents: float,
    contracts: int,
    stake: float,
    fee: float,
    strike: float,
    close_price: float,
) -> tuple[str, float, float]:
    """
    Returns (outcome, payout_dollars, pnl_dollars).
    YES wins if close > strike; NO wins if close < strike.
    Exactly-at-strike treats YES as loss (conservative).
    """
    winning_side = "yes" if close_price > strike else "no"
    if side == winning_side:
        payout = float(contracts) * 1.0
        pnl = payout - stake - fee
        return "win", payout, pnl
    return "loss", 0.0, -(stake + fee)


def run_backtest(
    strategy: BaseStrategy,
    events: Iterable[BacktestEvent],
    stake_dollars: float = 5.0,
    maker_mode: bool = False,
    one_trade_per_window: bool = True,
    btc_price_lookup=None,
) -> list:
    """
    Run strategy against a stream of events and return a list of
    BacktestTrade records.
    """
    trades = []
    traded_windows: set = set()

    for event in events:
        if one_trade_per_window:
            window_key = (event.asset, event.window_start_ts)
            if window_key in traded_windows:
                continue

        features = event_to_features(event)

        if btc_price_lookup is not None and event.asset != "BTC":
            btc_p = btc_price_lookup(event.eval_ts)
            if btc_p is not None:
                features.btc_price = btc_p

        # Populate bot.btc_prices for non-BTC strategies that read it.
        # Non-BTC assets gracefully degrade (zero beta contribution)
        # since we only have the asset's own price history here.
        try:
            import bot
            bot.btc_prices.clear()
            if event.asset == "BTC":
                for ts, p in event.price_history:
                    bot.btc_prices.append((ts, p))
        except Exception:
            pass

        decision = strategy.decide(features)
        if decision.action == "skip":
            continue

        side = decision.side
        entry_cents = (
            event.orderbook.yes_ask if side == "yes"
            else event.orderbook.no_ask
        )
        entry_price_dollars = entry_cents / 100.0
        if entry_price_dollars <= 0 or entry_price_dollars >= 1.0:
            continue

        contracts = max(1, int(stake_dollars / entry_price_dollars))
        stake = contracts * entry_price_dollars
        fee_fn = maker_fee if maker_mode else taker_fee
        fee = fee_fn(contracts, entry_price_dollars)

        outcome, payout, pnl = _settle_trade(
            side=side,
            entry_cents=entry_cents,
            contracts=contracts,
            stake=stake,
            fee=fee,
            strike=event.strike,
            close_price=event.close_price,
        )

        if one_trade_per_window:
            traded_windows.add((event.asset, event.window_start_ts))

        trades.append(BacktestTrade(
            asset=event.asset,
            eval_ts=event.eval_ts,
            window_start_ts=event.window_start_ts,
            window_close_ts=event.window_close_ts,
            side=side,
            entry_price_cents=entry_cents,
            raw_p_model=float(
                decision.contributing_signals.get("raw_p_yes",
                                                  decision.p_model)
            ),
            calibrated_p_model=float(decision.p_model),
            contracts=contracts,
            stake_dollars=stake,
            fee_dollars=fee,
            close_price=event.close_price,
            strike=event.strike,
            outcome=outcome,
            payout_dollars=payout,
            pnl_dollars=pnl,
            ev_at_entry=float(decision.expected_value or 0.0),
            reason=decision.reason,
            signals_dump=dict(decision.contributing_signals),
        ))

    return trades
