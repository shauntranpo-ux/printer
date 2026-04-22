"""
Trading performance metrics.
All P&L figures are in dollars, per-trade.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional


def sharpe_ratio(pnls: np.ndarray, periods_per_year: float = 252 * 96.0) -> float:
    """
    Annualized Sharpe ratio from per-trade P&L series.
    periods_per_year: 252 days × 96 15-min periods/day.
    """
    if len(pnls) < 2:
        return float("nan")
    mean = float(np.mean(pnls))
    std  = float(np.std(pnls, ddof=1))
    if std == 0.0:
        return float("nan")
    return float(mean / std * np.sqrt(periods_per_year))


def daily_sharpe(daily_pnls: np.ndarray, periods_per_year: float = 252.0) -> float:
    if len(daily_pnls) < 2:
        return float("nan")
    mean = float(np.mean(daily_pnls))
    std  = float(np.std(daily_pnls, ddof=1))
    if std == 0.0:
        return float("nan")
    return float(mean / std * np.sqrt(periods_per_year))


def win_rate(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return float("nan")
    return float((pnls > 0).mean())


def expectancy(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return float("nan")
    return float(np.mean(pnls))


def max_drawdown(cumulative_pnl: np.ndarray) -> float:
    if len(cumulative_pnl) == 0:
        return float("nan")
    running_max = np.maximum.accumulate(cumulative_pnl)
    drawdowns = cumulative_pnl - running_max
    return float(np.min(drawdowns))


def avg_drawdown_duration(cumulative_pnl: np.ndarray) -> float:
    if len(cumulative_pnl) < 2:
        return float("nan")
    running_max = np.maximum.accumulate(cumulative_pnl)
    in_drawdown = cumulative_pnl < running_max
    durations: list[int] = []
    current = 0
    for flag in in_drawdown:
        if flag:
            current += 1
        else:
            if current > 0:
                durations.append(current)
            current = 0
    if current > 0:
        durations.append(current)
    return float(np.mean(durations)) if durations else 0.0


def fee_drag(total_fees: float, gross_pnl: float) -> float:
    if gross_pnl == 0:
        return float("nan")
    return total_fees / gross_pnl


def trading_summary(
    trade_log: pd.DataFrame,
    regime: Optional[str] = None,
) -> dict:
    """
    Compute full trading summary from a trade log DataFrame.
    Required columns: pnl, fee (optional), side, entry_time, exit_time
    """
    if trade_log.empty:
        return {"regime": regime or "all", "n_trades": 0}

    pnls = trade_log["pnl"].values
    fees = trade_log["fee"].values if "fee" in trade_log.columns else np.zeros(len(pnls))
    net_pnls = pnls - fees
    cum_pnl = np.cumsum(net_pnls)

    return {
        "regime":            regime or "all",
        "n_trades":          int(len(pnls)),
        "sharpe":            sharpe_ratio(net_pnls),
        "win_rate":          win_rate(net_pnls),
        "expectancy":        expectancy(net_pnls),
        "total_pnl":         float(net_pnls.sum()),
        "total_gross_pnl":   float(pnls.sum()),
        "total_fees":        float(fees.sum()),
        "fee_drag":          fee_drag(float(fees.sum()), float(pnls.sum())),
        "max_drawdown":      max_drawdown(cum_pnl),
        "avg_drawdown_dur":  avg_drawdown_duration(cum_pnl),
    }
