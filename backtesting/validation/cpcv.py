"""
Combinatorial Purged Cross-Validation (CPCV).

Parameters:
    N = 6 groups, k = 2 test groups
    → C(6, 2) = 15 splits
    → (k · C(N,k)) / N = (2 · 15) / 6 = 5 distinct backtest paths

Purging:
    Remove training obs whose label window overlaps the test window.
    Label horizon = 15 minutes.

Embargo:
    Drop embargo_minutes after each test fold before training resumes.
    Floor: must be >= label_horizon (15 min). Default: 30 min.
"""
from __future__ import annotations
import math
from itertools import combinations
from typing import Iterator
import numpy as np
import pandas as pd


LABEL_HORIZON_MINUTES = 15


def _n_combinations(n: int, k: int) -> int:
    return math.comb(n, k)


def _n_paths(n: int, k: int) -> int:
    """Number of distinct backtest paths = (k · C(N,k)) / N."""
    return (k * math.comb(n, k)) // n


def split_into_groups(
    timestamps: pd.DatetimeIndex,
    n_groups: int,
) -> list[pd.DatetimeIndex]:
    """Split a sorted timestamp index into N approximately equal groups."""
    splits = np.array_split(np.arange(len(timestamps)), n_groups)
    return [timestamps[s] for s in splits]


def get_cpcv_splits(
    timestamps: pd.DatetimeIndex,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_minutes: int = 30,
    label_horizon_minutes: int = LABEL_HORIZON_MINUTES,
) -> Iterator[tuple[pd.DatetimeIndex, pd.DatetimeIndex]]:
    """
    Yield (train_timestamps, test_timestamps) for each CPCV split.

    Purging and embargo are applied to training timestamps.
    Total splits = C(n_groups, k_test_groups).
    """
    if embargo_minutes < label_horizon_minutes:
        raise ValueError(
            f"embargo_minutes ({embargo_minutes}) must be >= label_horizon_minutes "
            f"({label_horizon_minutes})"
        )

    groups = split_into_groups(timestamps, n_groups)

    for test_group_indices in combinations(range(n_groups), k_test_groups):
        # Build test timestamps
        test_parts = [groups[i] for i in test_group_indices]
        test_timestamps = pd.DatetimeIndex(
            pd.concat([p.to_series() for p in test_parts]).sort_values()
        )

        test_start = test_timestamps.min()
        test_end   = test_timestamps.max()

        # Embargo window: observations within embargo_minutes after test end
        embargo_end = test_end + pd.Timedelta(minutes=embargo_minutes)

        # Purge: remove training obs whose label window overlaps test
        # A training obs at t overlaps test if t + label_horizon > test_start
        # i.e., keep only t < test_start - label_horizon OR t > embargo_end
        purge_cutoff = test_start - pd.Timedelta(minutes=label_horizon_minutes)

        train_parts = [groups[i] for i in range(n_groups) if i not in test_group_indices]
        if not train_parts:
            yield pd.DatetimeIndex([]), test_timestamps
            continue

        train_timestamps_raw = pd.DatetimeIndex(
            pd.concat([p.to_series() for p in train_parts]).sort_values()
        )

        # Apply purging and embargo
        mask = (train_timestamps_raw < purge_cutoff) | (train_timestamps_raw > embargo_end)
        train_timestamps = train_timestamps_raw[mask]

        yield train_timestamps, test_timestamps


def run_cpcv(
    timestamps: pd.DatetimeIndex,
    labels: np.ndarray,
    features: np.ndarray,
    feature_names: list[str],
    model_config: dict,
    fees_config: dict,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_minutes: int = 30,
) -> pd.DataFrame:
    """
    Run full CPCV and return a DataFrame of per-split OOS metrics.

    Returns DataFrame with columns:
        split_id, n_train, n_test, test_start, test_end,
        sharpe, win_rate, brier, log_loss, n_trades
    """
    import sys, os
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)
    from strategy_a.model import StrategyAModel
    from backtesting.metrics.calibration import brier_score, log_loss_score
    from backtesting.metrics.trading import sharpe_ratio

    results = []
    split_id = 0

    ts_to_idx = {t: i for i, t in enumerate(timestamps)}

    for train_ts, test_ts in get_cpcv_splits(
        timestamps, n_groups, k_test_groups, embargo_minutes
    ):
        split_id += 1

        train_idx = np.array([ts_to_idx[t] for t in train_ts if t in ts_to_idx])
        test_idx  = np.array([ts_to_idx[t] for t in test_ts  if t in ts_to_idx])

        if len(train_idx) < 30 or len(test_idx) < 5:
            continue

        X_train, y_train = features[train_idx], labels[train_idx]
        X_test,  y_test  = features[test_idx],  labels[test_idx]

        model = StrategyAModel(model_config, fees_config)
        model.fit(X_train, y_train, feature_names)

        p_hat = np.array([
            model.predict_proba({n: v for n, v in zip(feature_names, row)})
            for row in X_test
        ])

        bs = brier_score(y_test, p_hat)
        ll = log_loss_score(y_test, p_hat)

        # Simplified trading metrics — assume trade when |edge| > min_edge
        # TODO: wire real Kalshi prices per window for accurate p_market
        p_market = 0.5
        edges = p_hat - p_market
        trade_mask = np.abs(edges) > 0.055  # approximate min_edge

        pnls = np.where(
            trade_mask,
            np.where(
                edges > 0,
                np.where(y_test == 1, 1.0 - p_hat, -p_hat),
                np.where(y_test == 0, 1.0 - (1.0 - p_hat), -(1.0 - p_hat)),
            ),
            0.0,
        )
        fee_drag = 0.03 * trade_mask.astype(float)
        net_pnls = pnls - fee_drag

        traded_pnls = net_pnls[trade_mask]
        sr = sharpe_ratio(traded_pnls) if len(traded_pnls) > 1 else float("nan")
        wr = float((traded_pnls > 0).mean()) if len(traded_pnls) > 0 else float("nan")

        results.append({
            "split_id":   split_id,
            "n_train":    int(len(train_idx)),
            "n_test":     int(len(test_idx)),
            "test_start": test_ts.min(),
            "test_end":   test_ts.max(),
            "sharpe":     sr,
            "win_rate":   wr,
            "brier":      bs,
            "log_loss":   ll,
            "n_trades":   int(trade_mask.sum()),
        })

    return pd.DataFrame(results)
