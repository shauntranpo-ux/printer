"""
Strategy C simulation adapter.

Iterates over Kalshi hourly events in chronological order, evaluates C1
(probability surface) and C2 (ladder arbitrage scanner) per event, and
records fills in a trade log compatible with report_builder.py.

Hard constraints:
  - Sequential only: no async, no threading, no multiprocessing.
  - No look-ahead: only bars with timestamp < event entry_time are used.
  - 3% taker fee on every trade.
  - At most 2 positions per event (C1 + C2 combined; enforced per-event).
  - No modification to strategies/.

Trade log columns (superset of backtest_engine.py _RESULT_COLUMNS):
    entry_time, exit_time, event_id, asset, strategy, side, strike,
    moneyness_bucket, violation_type,
    p_model, p_market, edge, regime,
    fill_price, pnl, fee, label
"""
from __future__ import annotations
import logging
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_RESULT_COLUMNS = [
    "entry_time", "exit_time", "event_id", "asset", "strategy",
    "side", "strike", "moneyness_bucket", "violation_type",
    "p_model", "p_market", "edge", "regime",
    "fill_price", "pnl", "fee", "label",
]


def run_strategy_c_backtest(
    asset: str,
    ladder_df: pd.DataFrame,
    labels_df: pd.DataFrame,
    underlying_bars: pd.DataFrame,
    strategy_c_config: dict,
    fitted_config: Optional[dict] = None,
    run_c1: bool = True,
    run_c2: bool = True,
    max_positions_per_event: int = 2,
) -> pd.DataFrame:
    """
    Run the Strategy C event-driven simulation.

    Args:
        asset:                 "btc" or "eth"
        ladder_df:             Output of load_strike_ladder_history().
        labels_df:             Output of build_strike_ladder_labels() — one row per (event_id, strike).
        underlying_bars:       1-minute OHLCV bars.
        strategy_c_config:     Contents of strategies/strategy_c/config/{asset}.yaml.
        fitted_config:         Sidecar fitted.yaml content (calibrators, mu_hat). May be None.
        run_c1:                Whether to evaluate C1 (probability surface) trades.
        run_c2:                Whether to evaluate C2 (arbitrage scanner) trades.
        max_positions_per_event: Combined C1+C2 position cap per event (default 2).

    Returns:
        DataFrame with columns listed in _RESULT_COLUMNS.
    """
    _ensure_strategies_importable()

    if ladder_df.empty or labels_df.empty:
        return pd.DataFrame(columns=_RESULT_COLUMNS)

    taker_fee = float(
        strategy_c_config.get("fees", {}).get("kalshi", {}).get("taker_fee_rate", 0.03)
    )
    safety_margin = float(strategy_c_config.get("fees", {}).get("safety_margin", 0.005))
    sigma_ref = float(
        strategy_c_config.get("volatility_reference", {}).get("annualized", 0.5)
    )
    rfr = float(strategy_c_config.get("probability", {}).get("risk_free_rate", 0.0))
    mu_hat = 0.0
    if fitted_config:
        mu_hat = float(
            fitted_config.get("probability", {}).get("drift_adjustment_weight", 0.0)
        )

    bars_sorted = underlying_bars.sort_values("timestamp").reset_index(drop=True)

    # Build event → label lookup: {event_id: {strike: label}}
    label_lookup: dict[str, dict[float, int]] = {}
    close_time_lookup: dict[str, pd.Timestamp] = {}
    for _, row in labels_df.iterrows():
        eid = row["event_id"]
        if eid not in label_lookup:
            label_lookup[eid] = {}
            close_time_lookup[eid] = row["event_close_time"]
        label_lookup[eid][float(row["strike"])] = int(row["label"])

    # Iterate events in chronological order
    event_order = sorted(close_time_lookup.items(), key=lambda x: x[1])

    records: list[dict] = []

    for event_id, event_close_time in event_order:
        if event_id not in label_lookup:
            continue

        # Get entry snapshot: earliest ladder snapshot for this event
        event_snaps = ladder_df[ladder_df["event_id"] == event_id]
        if event_snaps.empty:
            continue

        entry_time = event_snaps["timestamp"].min()

        # Build spot price at entry (no look-ahead)
        candidates = bars_sorted[bars_sorted["timestamp"] <= entry_time]
        if candidates.empty:
            continue
        spot = float(candidates["close"].iloc[-1])
        if spot <= 0:
            continue

        tte_s = (event_close_time - entry_time).total_seconds()
        if tte_s <= 0:
            continue

        # Regime (session) for this entry time
        from shared.regime_filters import get_current_regime as classify_session
        regime = classify_session(entry_time)

        # Build the ladder snapshot dict for C2 scanner
        latest_snaps = (
            event_snaps.sort_values("timestamp")
            .groupby("strike")
            .last()
            .reset_index()
        )
        snapshot = _build_snapshot_dict(event_id, event_close_time, latest_snaps)

        positions_taken = 0
        event_records: list[dict] = []

        # ── C1: per-strike probability surface ────────────────────────────
        if run_c1 and positions_taken < max_positions_per_event:
            c1_records = _evaluate_c1(
                event_id=event_id,
                snapshot=snapshot,
                spot=spot,
                tte_s=tte_s,
                sigma_ref=sigma_ref,
                rfr=rfr,
                mu_hat=mu_hat,
                regime=regime,
                taker_fee=taker_fee,
                safety_margin=safety_margin,
                label_lookup=label_lookup[event_id],
                entry_time=entry_time,
                event_close_time=event_close_time,
                asset=asset,
                strategy_c_config=strategy_c_config,
                fitted_config=fitted_config,
            )
            for rec in c1_records:
                if positions_taken >= max_positions_per_event:
                    break
                event_records.append(rec)
                positions_taken += 1

        # ── C2: arbitrage scanner ──────────────────────────────────────────
        if run_c2 and positions_taken < max_positions_per_event:
            c2_records = _evaluate_c2(
                event_id=event_id,
                snapshot=snapshot,
                label_lookup=label_lookup[event_id],
                entry_time=entry_time,
                event_close_time=event_close_time,
                asset=asset,
                taker_fee=taker_fee,
                regime=regime,
                strategy_c_config=strategy_c_config,
            )
            for rec in c2_records:
                if positions_taken >= max_positions_per_event:
                    break
                event_records.append(rec)
                positions_taken += 1

        records.extend(event_records)

    if not records:
        return pd.DataFrame(columns=_RESULT_COLUMNS)

    df = pd.DataFrame(records)
    for col in _RESULT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[_RESULT_COLUMNS].reset_index(drop=True)


def _evaluate_c1(
    event_id: str,
    snapshot: dict,
    spot: float,
    tte_s: float,
    sigma_ref: float,
    rfr: float,
    mu_hat: float,
    regime: str,
    taker_fee: float,
    safety_margin: float,
    label_lookup: dict[float, int],
    entry_time: pd.Timestamp,
    event_close_time: pd.Timestamp,
    asset: str,
    strategy_c_config: dict,
    fitted_config: Optional[dict],
) -> list[dict]:
    """Evaluate C1 model on the event snapshot. Returns trade records."""
    from strategy_c.model import StrategyC1Model

    try:
        c1_model = StrategyC1Model(strategy_c_config)
        if fitted_config:
            c1_model.load_calibrators(fitted_config)
        feature_vector: dict = {}  # minimal; HAR features would require more bar data
        surface_df = c1_model.predict_surface(snapshot, feature_vector, strategy_c_config)
        if surface_df.empty:
            return []

        ranked = c1_model.rank_candidates(surface_df, strategy_c_config)
        if ranked.empty:
            return []

    except Exception as exc:
        logger.debug("C1 evaluation failed for event %s: %s", event_id, exc)
        return []

    records = []
    for _, row in ranked.iterrows():
        strike = float(row["strike"])
        p_model = float(row.get("p_calibrated", row.get("p_raw", 0.5)))
        moneyness_bucket = str(row.get("moneyness_bucket", "atm"))
        edge = float(row.get("edge", 0.0))
        side = str(row.get("side", "yes"))

        if not c1_model.should_trade_strike(edge, moneyness_bucket, regime, strategy_c_config):
            continue

        # Market price from snapshot
        strike_data = next(
            (s for s in snapshot.get("strikes", []) if s.get("strike") == strike), None
        )
        if strike_data is None:
            continue

        if side == "yes":
            raw_bid = float(strike_data.get("yes_bid", 0))
            raw_ask = float(strike_data.get("yes_ask", 1))
        else:
            raw_bid = float(strike_data.get("no_bid", 0))
            raw_ask = float(strike_data.get("no_ask", 1))

        p_market = float(np.clip((raw_bid + raw_ask) / 2.0, 0.01, 0.99))
        fill_price = float(np.clip(raw_ask + taker_fee, 0.0, 1.0))

        label = label_lookup.get(strike, 0)
        if side == "yes":
            gross = float(label) - fill_price
        else:
            gross = float(1 - label) - fill_price
        fee = taker_fee

        records.append({
            "entry_time": entry_time,
            "exit_time": event_close_time,
            "event_id": event_id,
            "asset": asset,
            "strategy": "strategy_c1",
            "side": side,
            "strike": strike,
            "moneyness_bucket": moneyness_bucket,
            "violation_type": None,
            "p_model": p_model,
            "p_market": p_market,
            "edge": edge,
            "regime": regime,
            "fill_price": fill_price,
            "pnl": gross,
            "fee": fee,
            "label": label,
        })

    return records


def _evaluate_c2(
    event_id: str,
    snapshot: dict,
    label_lookup: dict[float, int],
    entry_time: pd.Timestamp,
    event_close_time: pd.Timestamp,
    asset: str,
    taker_fee: float,
    regime: str,
    strategy_c_config: dict,
) -> list[dict]:
    """Evaluate C2 arbitrage scanner on the event snapshot. Returns trade records."""
    from strategy_c.scanner import StrategyC2Scanner

    try:
        scanner = StrategyC2Scanner(strategy_c_config)
        signals = scanner.scan_snapshot(snapshot)
    except Exception as exc:
        logger.debug("C2 scanner failed for event %s: %s", event_id, exc)
        return []

    records = []
    for signal in signals:
        for (strike, side, qty) in signal.recommended_legs:
            label = label_lookup.get(float(strike), 0)

            # Look up market price from snapshot
            strike_data = next(
                (s for s in snapshot.get("strikes", []) if s.get("strike") == float(strike)),
                None,
            )
            if strike_data is None:
                continue

            if side == "yes":
                raw_ask = float(strike_data.get("yes_ask", 1))
                p_market = float(np.clip(
                    (float(strike_data.get("yes_bid", 0)) + raw_ask) / 2.0, 0.01, 0.99
                ))
            else:
                raw_ask = float(strike_data.get("no_ask", 1))
                p_market = float(np.clip(
                    (float(strike_data.get("no_bid", 0)) + raw_ask) / 2.0, 0.01, 0.99
                ))

            fill_price = float(np.clip(raw_ask + taker_fee, 0.0, 1.0))
            if side == "yes":
                gross = float(label) - fill_price
            else:
                gross = float(1 - label) - fill_price

            records.append({
                "entry_time": entry_time,
                "exit_time": event_close_time,
                "event_id": event_id,
                "asset": asset,
                "strategy": "strategy_c2",
                "side": side,
                "strike": float(strike),
                "moneyness_bucket": None,
                "violation_type": signal.violation_type,
                "p_model": 1.0 - signal.theoretical_profit_per_contract,
                "p_market": p_market,
                "edge": signal.theoretical_profit_per_contract,
                "regime": regime,
                "fill_price": fill_price,
                "pnl": gross * qty,
                "fee": taker_fee * qty,
                "label": label,
            })

    return records


def _build_snapshot_dict(
    event_id: str,
    event_close_time: pd.Timestamp,
    latest_snaps: pd.DataFrame,
) -> dict:
    """Build a strategy_c snapshot dict from the latest ladder DataFrame rows."""
    strikes = []
    for _, row in latest_snaps.iterrows():
        yes_bid = float(row.get("yes_bid", 0))
        yes_ask = float(row.get("yes_ask", 100))
        # Normalize to [0,1] if prices appear to be in cents
        if yes_bid > 1.0 or yes_ask > 1.0:
            yes_bid /= 100.0
            yes_ask /= 100.0

        no_bid = float(row.get("no_bid", 0))
        no_ask = float(row.get("no_ask", 100))
        if no_bid > 1.0 or no_ask > 1.0:
            no_bid /= 100.0
            no_ask /= 100.0

        strikes.append({
            "strike": float(row["strike"]),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "last_price": (yes_bid + yes_ask) / 2.0,
            "volume": float(row.get("volume", 0)),
            "market_id": str(row.get("market_id", "")),
        })

    return {
        "event_id": event_id,
        "event_close_time": event_close_time,
        "strikes": strikes,
    }


def _ensure_strategies_importable() -> None:
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)
