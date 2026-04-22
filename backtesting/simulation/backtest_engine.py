"""
Event-driven single-threaded backtest engine for Kalshi 15-min binary contracts.

Hard constraints:
- No async, no multiprocessing, no threading.
- No vectorization of the main event loop.
- strategies/ must be imported at function call time via sys.path (NOT at module level).
- Never modify strategies/.
- 3% fee applied on every taker trade.
- No look-ahead: engine only sees events with timestamp < window_ts.
"""
from __future__ import annotations
import sys
import os
import numpy as np
import pandas as pd
from typing import Optional

from backtesting.data.aligner import Event, iter_windows
from backtesting.simulation.fill_model import TakerFillModel, MakerFillModel

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

_RESULT_COLUMNS = [
    "entry_time", "exit_time", "asset", "strategy", "side",
    "p_model", "p_market", "edge", "regime",
    "fill_price", "pnl", "fee", "label",
]


def run_backtest(
    events: list[Event],
    labels: pd.DataFrame,
    asset: str,
    strategy: str,
    model_a=None,
    model_b=None,
    model_config: dict | None = None,
    fees_config: dict | None = None,
    fill_model_type: str = "taker",
    latency_ms: float = 500.0,
) -> pd.DataFrame:
    """
    Run the event-driven backtest loop.

    Args:
        events: sorted Event list from build_event_stream()
        labels: DataFrame with columns [timestamp, label, reference_price_open,
                reference_price_close, log_return] (UTC-aware timestamps)
        asset: e.g. "btc"
        strategy: "strategy_a" | "strategy_b" | "both"
        model_a: fitted StrategyAModel instance (optional; if None, skips strategy_a)
        model_b: fitted ContractDislocationDetector instance (optional)
        model_config: config dict for should_trade
        fees_config: fees dict for should_trade
        fill_model_type: "taker" or "maker"
        latency_ms: fill latency in milliseconds

    Returns:
        pd.DataFrame with columns:
            entry_time, exit_time, asset, strategy, side, p_model, p_market,
            edge, regime, fill_price, pnl, fee, label

    P&L formula (per unit, [0,1] space):
        - side="yes": pnl = label - fill_price
        - side="no":  pnl = (1 - label) - fill_price
    """
    _ensure_strategies_importable()
    from shared.regime_filters import get_current_regime as classify_session

    _model_config = model_config or {}
    _fees_config = fees_config or {
        "kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00},
        "safety_margin": 0.005,
    }

    if fill_model_type == "maker":
        filler = MakerFillModel()
    else:
        filler = TakerFillModel(latency_ms=latency_ms)

    if labels.empty:
        return pd.DataFrame(columns=_RESULT_COLUMNS)

    label_ts_index = pd.DatetimeIndex(labels["timestamp"])
    label_lookup = dict(zip(labels["timestamp"], labels["label"]))

    records: list[dict] = []

    for window_ts, events_before in iter_windows(events, label_ts_index):
        label = label_lookup.get(window_ts)
        if label is None:
            continue

        # Partition events by type
        bars_events = [e for e in events_before if e.event_type == "bar"]
        kalshi_events = [e for e in events_before if e.event_type == "kalshi_tick"]
        trade_events = [e for e in events_before if e.event_type == "trade"]

        # Build kalshi_ticks list sorted by timestamp
        kalshi_sorted = sorted(kalshi_events, key=lambda e: e.timestamp)
        kalshi_ticks: list[dict] = []
        for e in kalshi_sorted:
            tick = dict(e.payload)
            if "timestamp" not in tick:
                tick["timestamp"] = e.timestamp
            kalshi_ticks.append(tick)

        # Get p_market from latest Kalshi tick mid (in [0,1])
        if kalshi_ticks:
            last_tick = kalshi_ticks[-1]
            yes_mid = (
                last_tick.get("yes_bid", 50) + last_tick.get("yes_ask", 50)
            ) / 200.0
            p_market = float(np.clip(yes_mid, 0.01, 0.99))
        else:
            p_market = 0.5

        regime = classify_session(window_ts)
        exit_time = window_ts + pd.Timedelta(minutes=15)

        # ── Strategy A ────────────────────────────────────────────────────────
        if strategy in ("strategy_a", "both") and model_a is not None:
            features = _build_features_from_bars(bars_events)
            p_model = model_a.predict_proba(features)
            edge = model_a.get_edge(p_model, p_market)

            if model_a.should_trade(p_model, p_market, regime, _model_config):
                side = "yes" if edge > 0 else "no"

                if not kalshi_ticks:
                    # No ticks: use p_market as fill price, apply filler fee_rate
                    fp_fallback = p_market if side == "yes" else (1 - p_market)
                    fill = {
                        "fill_price": fp_fallback,
                        "fee": filler.fee_rate * fp_fallback,
                        "slippage": 0.0,
                        "timestamp_filled": window_ts,
                    }
                else:
                    fill = filler.fill(side, window_ts, kalshi_ticks)

                if fill is not None:
                    fp = fill["fill_price"]
                    pnl = (label - fp) if side == "yes" else ((1 - label) - fp)

                    records.append({
                        "entry_time": window_ts,
                        "exit_time": exit_time,
                        "asset": asset,
                        "strategy": "strategy_a",
                        "side": side,
                        "p_model": p_model,
                        "p_market": p_market,
                        "edge": edge,
                        "regime": regime,
                        "fill_price": fp,
                        "pnl": pnl,
                        "fee": fill["fee"],
                        "label": int(label),
                    })

        # ── Strategy B ────────────────────────────────────────────────────────
        if strategy in ("strategy_b", "both") and model_b is not None:
            underlying_stream = [
                {
                    "timestamp": e.timestamp,
                    "price": e.payload.get("close", e.payload.get("price", 0.0)),
                }
                for e in sorted(bars_events, key=lambda e: e.timestamp)
            ]

            if not underlying_stream:
                underlying_stream = [
                    {
                        "timestamp": e.timestamp,
                        "price": e.payload.get("price", 0.0),
                    }
                    for e in sorted(trade_events, key=lambda e: e.timestamp)
                ]

            # Build contract stream
            contract_stream: list[dict] = []
            for e in sorted(kalshi_events, key=lambda e: e.timestamp):
                tick = dict(e.payload)
                if "timestamp" not in tick:
                    tick["timestamp"] = e.timestamp
                contract_stream.append(tick)

            signal_b = model_b.detect_dislocation(contract_stream, underlying_stream)

            if signal_b is not None:
                side = signal_b.side
                if not kalshi_ticks:
                    # No ticks: use p_market as fill price, apply filler fee_rate
                    fp_fallback = p_market if side == "yes" else (1 - p_market)
                    fill = {
                        "fill_price": fp_fallback,
                        "fee": filler.fee_rate * fp_fallback,
                        "slippage": 0.0,
                        "timestamp_filled": window_ts,
                    }
                else:
                    fill = filler.fill(side, window_ts, kalshi_ticks)

                if fill is not None:
                    fp = fill["fill_price"]
                    pnl = (label - fp) if side == "yes" else ((1 - label) - fp)

                    records.append({
                        "entry_time": window_ts,
                        "exit_time": exit_time,
                        "asset": asset,
                        "strategy": "strategy_b",
                        "side": side,
                        "p_model": signal_b.confidence,
                        "p_market": p_market,
                        "edge": signal_b.residual_magnitude,
                        "regime": regime,
                        "fill_price": fp,
                        "pnl": pnl,
                        "fee": fill["fee"],
                        "label": int(label),
                    })

    if records:
        return pd.DataFrame(records, columns=_RESULT_COLUMNS)
    return pd.DataFrame(columns=_RESULT_COLUMNS)


def _ensure_strategies_importable() -> None:
    strategies_path = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "strategies"))
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)


def _build_features_from_bars(bar_events: list[Event]) -> dict:
    """
    Build a HAR-style feature dict from bar events.
    Returns empty dict if insufficient data.
    Feature keys follow FeatureVector.to_flat_dict() convention:
    "har_rv__rv_15m_pos", etc.
    """
    if len(bar_events) < 1440:  # need at least 240 minutes of 10-second bars
        return {}

    from backtesting.training.har_fitter import _rv_components, _bars_in_window

    closes = np.array(
        [e.payload.get("close", 0.0) for e in sorted(bar_events, key=lambda e: e.timestamp)]
    )
    closes = closes[closes > 0]
    if len(closes) < 2:
        return {}

    log_rets = np.log(closes[1:] / closes[:-1])

    features: dict[str, float] = {}
    for t_min, alias in [(15, "rv_15m"), (60, "rv_1h"), (240, "rv_4h")]:
        n_bars = _bars_in_window(10, t_min)
        if len(log_rets) < n_bars:
            return {}
        comps = _rv_components(log_rets[-n_bars:])
        features[f"har_rv__{alias}_pos"] = comps["rv_pos"]
        features[f"har_rv__{alias}_neg"] = comps["rv_neg"]

    jump_n = _bars_in_window(10, 15)
    features["har_rv__jump_15m"] = _rv_components(log_rets[-jump_n:])["jump"]

    return features
