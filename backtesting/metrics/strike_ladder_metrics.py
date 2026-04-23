"""
Strike-ladder metrics for Strategy C backtesting.

Provides:
  - Per-moneyness calibration breakdowns (Brier, ECE, log-loss per bucket)
  - Per-event P&L aggregation
  - C2 (arbitrage scanner) trade summary
  - Combined C1+C2 event-level summary
"""
from __future__ import annotations
import logging
from typing import Optional

import numpy as np
import pandas as pd

from backtesting.metrics.calibration import (
    brier_score,
    log_loss_score,
    expected_calibration_error,
)
from backtesting.metrics.trading import sharpe_ratio, win_rate, expectancy

logger = logging.getLogger(__name__)

_MONEYNESS_BUCKETS = ["deep_itm", "itm", "atm", "otm", "deep_otm"]


def per_moneyness_calibration(
    trade_log: pd.DataFrame,
    y_col: str = "label",
    p_col: str = "p_model",
    bucket_col: str = "moneyness_bucket",
) -> pd.DataFrame:
    """
    Compute calibration metrics broken down by moneyness bucket.

    Args:
        trade_log:  DataFrame with at least [label, p_model, moneyness_bucket].
        y_col:      Column name for realized binary outcome.
        p_col:      Column name for model probability.
        bucket_col: Column name for moneyness bucket.

    Returns:
        DataFrame with one row per bucket:
            moneyness_bucket, n, brier, log_loss, ece, mean_p_model, mean_label
    """
    if trade_log.empty or bucket_col not in trade_log.columns:
        return pd.DataFrame(columns=[
            "moneyness_bucket", "n", "brier", "log_loss", "ece",
            "mean_p_model", "mean_label",
        ])

    rows = []
    for bucket in _MONEYNESS_BUCKETS:
        sub = trade_log[trade_log[bucket_col] == bucket]
        if sub.empty:
            rows.append({
                "moneyness_bucket": bucket,
                "n": 0,
                "brier": float("nan"),
                "log_loss": float("nan"),
                "ece": float("nan"),
                "mean_p_model": float("nan"),
                "mean_label": float("nan"),
            })
            continue
        y = sub[y_col].values.astype(float)
        p = sub[p_col].values.astype(float)
        rows.append({
            "moneyness_bucket": bucket,
            "n": int(len(y)),
            "brier": brier_score(y, p),
            "log_loss": log_loss_score(y, p),
            "ece": expected_calibration_error(y, p),
            "mean_p_model": float(np.nanmean(p)),
            "mean_label": float(np.nanmean(y)),
        })

    return pd.DataFrame(rows)


def per_event_pnl(
    trade_log: pd.DataFrame,
    event_col: str = "event_id",
    pnl_col: str = "pnl",
    fee_col: str = "fee",
    strategy_col: str = "strategy",
) -> pd.DataFrame:
    """
    Aggregate per-event P&L from a Strategy C trade log.

    Returns one row per event_id:
        event_id, n_trades, gross_pnl, total_fee, net_pnl,
        c1_trades, c2_trades, win_rate
    """
    if trade_log.empty or event_col not in trade_log.columns:
        return pd.DataFrame(columns=[
            "event_id", "n_trades", "gross_pnl", "total_fee",
            "net_pnl", "c1_trades", "c2_trades", "win_rate",
        ])

    rows = []
    for eid, grp in trade_log.groupby(event_col):
        pnls = grp[pnl_col].values.astype(float) if pnl_col in grp.columns else np.zeros(len(grp))
        fees = grp[fee_col].values.astype(float) if fee_col in grp.columns else np.zeros(len(grp))
        net = pnls - fees

        c1_mask = grp[strategy_col].str.contains("c1", case=False, na=False) if strategy_col in grp.columns else pd.Series([True] * len(grp))
        c2_mask = grp[strategy_col].str.contains("c2", case=False, na=False) if strategy_col in grp.columns else pd.Series([False] * len(grp))

        rows.append({
            "event_id": eid,
            "n_trades": int(len(grp)),
            "gross_pnl": float(pnls.sum()),
            "total_fee": float(fees.sum()),
            "net_pnl": float(net.sum()),
            "c1_trades": int(c1_mask.sum()),
            "c2_trades": int(c2_mask.sum()),
            "win_rate": float((net > 0).mean()) if len(net) > 0 else float("nan"),
        })

    return pd.DataFrame(rows)


def c2_arbitrage_summary(
    trade_log: pd.DataFrame,
    strategy_col: str = "strategy",
    pnl_col: str = "pnl",
    fee_col: str = "fee",
    violation_col: str = "violation_type",
) -> dict:
    """
    Summarize Strategy C2 (arbitrage) trade performance.

    Returns dict with:
        n_c2_trades, n_monotonicity, n_convexity,
        gross_pnl, net_pnl, win_rate, mean_profit_per_trade
    """
    if trade_log.empty or strategy_col not in trade_log.columns:
        return {
            "n_c2_trades": 0,
            "n_monotonicity": 0,
            "n_convexity": 0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "win_rate": float("nan"),
            "mean_profit_per_trade": float("nan"),
        }

    c2 = trade_log[trade_log[strategy_col].str.contains("c2", case=False, na=False)]
    if c2.empty:
        return {
            "n_c2_trades": 0,
            "n_monotonicity": 0,
            "n_convexity": 0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "win_rate": float("nan"),
            "mean_profit_per_trade": float("nan"),
        }

    pnls = c2[pnl_col].values.astype(float) if pnl_col in c2.columns else np.zeros(len(c2))
    fees = c2[fee_col].values.astype(float) if fee_col in c2.columns else np.zeros(len(c2))
    net = pnls - fees

    n_mono = int(
        c2[violation_col].str.contains("mono", case=False, na=False).sum()
        if violation_col in c2.columns else 0
    )
    n_conv = int(
        c2[violation_col].str.contains("conv", case=False, na=False).sum()
        if violation_col in c2.columns else 0
    )

    return {
        "n_c2_trades": int(len(c2)),
        "n_monotonicity": n_mono,
        "n_convexity": n_conv,
        "gross_pnl": float(pnls.sum()),
        "net_pnl": float(net.sum()),
        "win_rate": float((net > 0).mean()),
        "mean_profit_per_trade": float(net.mean()),
    }


def strategy_c_full_summary(
    trade_log: pd.DataFrame,
    labels_df: Optional[pd.DataFrame] = None,
) -> dict:
    """
    Full Strategy C trading summary combining C1 and C2 metrics.

    Returns dict with:
        n_events_seen, n_events_traded,
        c1_* keys, c2_* keys,
        combined_sharpe, combined_win_rate, combined_net_pnl, fee_drag_pct
    """
    if trade_log.empty:
        return {"n_events_seen": 0, "n_events_traded": 0}

    pnl_col = "pnl" if "pnl" in trade_log.columns else None
    fee_col = "fee" if "fee" in trade_log.columns else None

    if pnl_col is None:
        return {"n_events_seen": 0, "n_events_traded": 0}

    pnls = trade_log[pnl_col].values.astype(float)
    fees = trade_log[fee_col].values.astype(float) if fee_col else np.zeros(len(pnls))
    net = pnls - fees

    n_events_traded = (
        trade_log["event_id"].nunique()
        if "event_id" in trade_log.columns
        else int(len(trade_log) > 0)
    )
    n_events_seen = int(labels_df["event_id"].nunique()) if labels_df is not None and not labels_df.empty else n_events_traded

    c2_summary = c2_arbitrage_summary(trade_log)

    strategy_col = "strategy" if "strategy" in trade_log.columns else None
    if strategy_col:
        c1 = trade_log[~trade_log[strategy_col].str.contains("c2", case=False, na=False)]
    else:
        c1 = trade_log

    c1_pnl = c1[pnl_col].values.astype(float) if not c1.empty else np.array([])
    c1_fee = c1[fee_col].values.astype(float) if (not c1.empty and fee_col) else np.zeros(len(c1_pnl))
    c1_net = c1_pnl - c1_fee

    gross_pnl = float(pnls.sum())
    total_fee = float(fees.sum())
    net_pnl = float(net.sum())

    return {
        "n_events_seen": n_events_seen,
        "n_events_traded": n_events_traded,
        "n_total_trades": int(len(trade_log)),
        "c1_trades": int(len(c1)),
        "c1_net_pnl": float(c1_net.sum()) if len(c1_net) > 0 else 0.0,
        "c1_win_rate": float((c1_net > 0).mean()) if len(c1_net) > 0 else float("nan"),
        "c2_trades": c2_summary["n_c2_trades"],
        "c2_net_pnl": c2_summary["net_pnl"],
        "c2_win_rate": c2_summary["win_rate"],
        "combined_sharpe": sharpe_ratio(net) if len(net) > 1 else float("nan"),
        "combined_win_rate": float((net > 0).mean()) if len(net) > 0 else float("nan"),
        "combined_net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "total_fee": total_fee,
        "fee_drag_pct": (total_fee / gross_pnl * 100.0) if gross_pnl != 0 else float("nan"),
    }
