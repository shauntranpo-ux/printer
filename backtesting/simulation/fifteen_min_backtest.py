"""
FifteenMinStrategy backtest — Supertrend ATR direction signal.

Pipeline per 15m window:
  1. Enter at `entry_minute` (default=5, 10 min left).
  2. Strike = close of bar 0 in the window.
  3. Build prices_60m deque from ≤120 bars ending at the entry bar.
  4. Synthetic market prices: yes_ask=50c / no_ask=50c (ATM baseline).
  5. Run FifteenMinStrategy.decide() — direction from Supertrend ATR.
  6. P&L vs actual window outcome (bar[14].close vs strike).

Includes: run_fifteen_min_backtest(), run_monte_carlo(), run_wfa()
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time as _time
from collections import deque as _deque
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")

_RESULT_COLUMNS = [
    "entry_time", "exit_time", "asset", "strategy",
    "side", "abs_pct", "mins_left",
    "yes_ask", "no_ask", "fill_price", "pnl", "fee", "label", "win",
]

# Per-asset min_ev matching config.json asset_overrides
_MIN_EV = {"BTC": 0.07, "ETH": 0.09, "SOL": 0.16, "XRP": 0.16}


def _setup_paths() -> None:
    for p in [_PROJECT_ROOT, _SRC_DIR]:
        if p not in sys.path:
            sys.path.insert(0, p)


def _kalshi_fee(fill_price: float) -> float:
    """Exact Kalshi taker fee per unit (1 contract)."""
    raw = 0.07 * fill_price * (1.0 - fill_price)
    return math.ceil(raw * 100) / 100  # round up to nearest cent


def run_fifteen_min_backtest(
    bars: pd.DataFrame,
    asset: str,
    confidence_threshold: float = 0.0,
    min_ev: Optional[float] = None,
    stake_dollars: float = 25.0,
    entry_minute: int = 5,
    yes_ask_cents: float = 50.0,
    no_ask_cents: float = 50.0,
    min_entry_price_cents: float = 20.0,
    max_entry_price_cents: float = 76.0,
    vol_ratio_threshold: float = 1.80,
    supertrend_atr_period: int = 10,
    supertrend_atr_multiplier: float = 3.0,
    strike_lookback_bars: int = 30,
) -> pd.DataFrame:
    """
    Simulate FifteenMinStrategy on historical 1m bars using Supertrend ATR for direction.

    Args:
        bars:           1m OHLCV DataFrame with timestamp, open, high, low, close, volume.
        asset:          Asset name (BTC/ETH/SOL/XRP).
        entry_minute:   Which bar within the 15m window to use as entry (default=5 → 10 min left).
        yes_ask_cents / no_ask_cents: Synthetic ATM market prices (default 50c each).
    """
    _setup_paths()

    from strategies.fifteen_min_strategy import FifteenMinStrategy
    from strategies.skip_layer import SkipConfig
    from strategies.features import MarketFeatures
    asset_upper = asset.upper()
    effective_min_ev = min_ev if min_ev is not None else _MIN_EV.get(asset_upper, 0.11)

    skip_cfg = SkipConfig(
        min_entry_price_cents=min_entry_price_cents,
        max_entry_price_cents=max_entry_price_cents,
        cold_start_samples=60,
        vol_ratio_threshold=vol_ratio_threshold,
    )
    strat = FifteenMinStrategy(
        asset=asset_upper,
        skip_config=skip_cfg,
        min_ev=effective_min_ev,
        stake_dollars=stake_dollars,
        confidence_threshold=confidence_threshold,
        supertrend_atr_period=supertrend_atr_period,
        supertrend_atr_multiplier=supertrend_atr_multiplier,
    )

    bars = bars.sort_values("timestamp").reset_index(drop=True)
    n = len(bars)
    step = 15  # 15 bars per window for 1m granularity
    records: list[dict] = []

    yes_ask = yes_ask_cents
    no_ask = no_ask_cents

    for start_idx in range(0, n - step + 1, step):
        window = bars.iloc[start_idx : start_idx + step]
        if len(window) < step:
            break

        strike_bar_idx = max(0, start_idx - strike_lookback_bars)
        strike_price  = float(bars.iloc[strike_bar_idx]["close"])
        entry_bar     = window.iloc[entry_minute]
        exit_bar      = window.iloc[-1]
        current_price = float(entry_bar["close"])
        exit_price    = float(exit_bar["close"])

        if strike_price <= 0 or current_price <= 0:
            continue

        abs_pct   = abs(current_price - strike_price) / current_price
        mins_left = float(step - entry_minute)

        # ── Supertrend feed: up to 120 1m bars ending at entry bar ───────────
        hist_start = max(0, start_idx + entry_minute - 120)
        hist_slice = bars.iloc[hist_start : start_idx + entry_minute]
        prices_60m: _deque = _deque(maxlen=3600)
        for _, hrow in hist_slice.iterrows():
            ts = pd.Timestamp(hrow["timestamp"])
            prices_60m.append((ts.timestamp(), float(hrow["close"])))

        # Realized vol from the same lookback window
        if len(hist_slice) >= 2:
            lr = np.log(
                hist_slice["close"].clip(lower=1e-10).values
                / hist_slice["open"].clip(lower=1e-10).values
            )
            rv_1min = float(np.std(lr))
        else:
            rv_1min = 0.002

        entry_ts = pd.Timestamp(entry_bar["timestamp"])

        features = MarketFeatures(
            asset=asset_upper,
            ticker=f"KX{asset_upper}15M-BT",
            timestamp=_time.time(),
            current_price=current_price,
            strike=strike_price,
            btc_price=current_price if asset_upper == "BTC" else 95000.0,
            seconds_left=mins_left * 60.0,
            elapsed_seconds=float(entry_minute * 60),
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=max(0.0, yes_ask - 1.0),
            no_bid=max(0.0, no_ask - 1.0),
            spread_yes=1.0,
            spread_no=1.0,
            realized_vol_1min=rv_1min,
        )
        features.prices_60m = prices_60m

        decision = strat.decide(features)
        if decision.action != "trade":
            continue

        label = 1 if exit_price > strike_price else 0
        fill_cents = yes_ask if decision.side == "yes" else no_ask
        fill_price = fill_cents / 100.0
        fee = _kalshi_fee(fill_price)

        pnl_raw = (label - fill_price) if decision.side == "yes" else ((1 - label) - fill_price)
        pnl_net = pnl_raw - fee

        win = int(
            (decision.side == "yes" and label == 1)
            or (decision.side == "no" and label == 0)
        )
        records.append({
            "entry_time": entry_ts,
            "exit_time":  pd.Timestamp(exit_bar["timestamp"]),
            "asset":      asset_upper,
            "strategy":   "15m_supertrend",
            "side":       decision.side,
            "abs_pct":    round(abs_pct * 100, 4),  # stored as percent
            "mins_left":  mins_left,
            "yes_ask":    yes_ask,
            "no_ask":     no_ask,
            "fill_price": round(fill_price, 4),
            "pnl":        round(pnl_net, 4),
            "fee":        round(fee, 4),
            "label":      label,
            "win":        win,
        })

    if not records:
        logger.warning("[%s] No trades generated (check BV3 table and threshold).", asset_upper)
        return pd.DataFrame(columns=_RESULT_COLUMNS)

    df = pd.DataFrame(records)
    logger.info(
        "[%s] Backtest: %d trades | WR=%.1f%% | total_pnl=%.4f | avg_pnl=%.4f",
        asset_upper, len(df),
        df["win"].mean() * 100,
        df["pnl"].sum(),
        df["pnl"].mean(),
    )
    return df


def run_monte_carlo(
    trade_log: pd.DataFrame,
    n_iterations: int = 1000,
    confidence_level: float = 0.95,
) -> pd.DataFrame:
    """
    Bootstrap Monte Carlo on a completed trade log.

    Resamples trades with replacement n_iterations times and computes
    the distribution of total P&L, win rate, and Sharpe ratio.
    """
    if trade_log.empty or "pnl" not in trade_log.columns:
        logger.warning("MC: empty trade log — skipping.")
        return pd.DataFrame()

    pnl = trade_log["pnl"].values
    n_trades = len(pnl)
    rng = np.random.default_rng(42)

    rows: list[dict] = []
    for i in range(n_iterations):
        sample = rng.choice(pnl, size=n_trades, replace=True)
        std = sample.std()
        rows.append({
            "iteration":  i,
            "total_pnl":  round(float(sample.sum()), 4),
            "win_rate":   round(float((sample > 0).mean()), 4),
            "sharpe":     round(float(sample.mean() / (std + 1e-10)), 4),
            "n_trades":   n_trades,
        })

    df = pd.DataFrame(rows)
    alpha = (1.0 - confidence_level) / 2.0
    lo, hi = np.quantile(df["total_pnl"], [alpha, 1.0 - alpha])
    logger.info(
        "MC (%d iters, n=%d): pnl_mean=%.4f (%.0f%% CI [%.4f, %.4f]) WR=%.3f sharpe=%.3f",
        n_iterations, n_trades,
        df["total_pnl"].mean(), confidence_level * 100, lo, hi,
        df["win_rate"].mean(), df["sharpe"].mean(),
    )
    return df


def run_wfa(
    bars: pd.DataFrame,
    asset: str,
    n_folds: int = 6,
    **backtest_kwargs,
) -> pd.DataFrame:
    """
    Walk-forward analysis: split bars into n_folds sequential time slices.
    Each slice runs an independent backtest, isolating regime variation.
    """
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    n = len(bars)
    fold_size = n // n_folds
    if fold_size < 30:
        logger.warning("WFA: too few bars for %d folds (total=%d); using 2.", n_folds, n)
        n_folds = 2
        fold_size = n // n_folds

    asset_upper = asset.upper()
    rows: list[dict] = []

    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else n
        fold = bars.iloc[start:end].reset_index(drop=True)

        fold_start = str(fold.iloc[0]["timestamp"])[:10]
        fold_end   = str(fold.iloc[-1]["timestamp"])[:10]

        try:
            tl = run_fifteen_min_backtest(fold, asset, **backtest_kwargs)
        except Exception as exc:
            logger.warning("[%s] WFA fold %d failed: %s", asset_upper, i, exc)
            tl = pd.DataFrame()

        if tl.empty:
            rows.append({
                "fold": i, "start": fold_start, "end": fold_end,
                "n_trades": 0, "total_pnl": 0.0, "win_rate": 0.0, "sharpe": 0.0,
            })
            continue

        pnl = tl["pnl"].values
        std = pnl.std()
        rows.append({
            "fold":      i,
            "start":     fold_start,
            "end":       fold_end,
            "n_trades":  len(pnl),
            "total_pnl": round(float(pnl.sum()), 4),
            "win_rate":  round(float((pnl > 0).mean()), 4),
            "sharpe":    round(float(pnl.mean() / (std + 1e-10)), 4),
        })
        logger.info(
            "[%s] WFA fold %d (%s→%s): %d trades WR=%.1f%% pnl=%.4f",
            asset_upper, i, fold_start, fold_end,
            len(pnl), (pnl > 0).mean() * 100, pnl.sum(),
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        logger.info(
            "[%s] WFA summary: avg WR=%.1f%% avg pnl=%.4f folds_positive=%d/%d",
            asset_upper,
            df["win_rate"].mean() * 100,
            df["total_pnl"].mean(),
            (df["total_pnl"] > 0).sum(), len(df),
        )
    return df
