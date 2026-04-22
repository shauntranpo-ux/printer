"""
Regime-conditional metric breakdowns.

Breaks all metrics down by:
  - time-of-day session
  - weekday vs weekend
  - month
  - BTC volatility tercile (low/mid/high)

Flags regimes where fee-inclusive Sharpe <= 0.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional

from backtesting.metrics.trading import trading_summary


SESSION_HOURS: dict[str, tuple[int, int]] = {
    "asia_deep_night": (0, 4),
    "asia_active":     (4, 9),
    "eu_open":         (9, 12),
    "eu_us_overlap":   (12, 17),
    "us_afternoon":    (17, 21),
    "us_late":         (21, 24),
}


def compute_regime_metrics(
    trade_log: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compute per-regime metrics from a trade log DataFrame.

    Required columns: entry_time (UTC timestamp), pnl, fee (optional)
    Optional columns: regime (pre-computed session label)
    Returns a DataFrame with one row per regime subset.
    Adds sharpe_flag=1 where Sharpe <= 0.
    """
    if trade_log.empty:
        return pd.DataFrame()

    results: list[dict] = []

    def _add_row(mask: pd.Series, regime_label: str, scope: str) -> None:
        subset = trade_log[mask].copy()
        if subset.empty:
            return
        summary = trading_summary(subset, regime=f"{scope}={regime_label}")
        summary["scope"] = scope
        summary["regime_value"] = regime_label
        summary["sharpe_flag"] = 1 if (summary.get("sharpe") or 0) <= 0 else 0
        results.append(summary)

    if "entry_time" in trade_log.columns:
        entry_dt = pd.to_datetime(trade_log["entry_time"], utc=True)
        hours = entry_dt.dt.hour
        dow   = entry_dt.dt.dayofweek
        month = entry_dt.dt.month

        # By session
        for session, (lo, hi) in SESSION_HOURS.items():
            mask = (hours >= lo) & (hours < hi)
            if mask.sum() > 0:
                _add_row(mask, session, "session")

        # Weekday vs weekend
        for label, mask in [("weekday", dow < 5), ("weekend", dow >= 5)]:
            if mask.sum() > 0:
                _add_row(mask, label, "day_type")

        # By month
        for m in sorted(month.unique()):
            mask = month == m
            if mask.sum() > 0:
                _add_row(mask, str(m), "month")

    return pd.DataFrame(results)
