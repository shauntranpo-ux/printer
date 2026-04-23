"""
Strategy C CPCV adapter — event-level splitting.

All 40 strikes from one Kalshi hourly event must be assigned to the same
CPCV fold. This adapter groups by event_id before splitting, runs
per-fold calibrator fitting, then evaluates held-out calibration and
per-strike P&L.

Label horizon: 1 hour (one full event window).
Embargo: at least 1 hour after each test fold boundary.

Output schema matches run_cpcv() in cpcv.py:
    split_id, n_train_events, n_test_events, test_start, test_end,
    sharpe, win_rate, brier, log_loss, n_trades,
    c2_violations_detected, c2_violations_traded
"""
from __future__ import annotations
import logging
import math
import os
import sys
from itertools import combinations
from typing import Iterator, Optional

import numpy as np
import pandas as pd

from backtesting.validation.cpcv import _n_combinations, _n_paths

logger = logging.getLogger(__name__)

LABEL_HORIZON_HOURS = 1
EMBARGO_HOURS = 1


def _split_events_into_groups(
    event_ids: list,
    event_close_times: pd.Series,
    n_groups: int,
) -> list[list]:
    """Split sorted event_ids into N approximately equal groups by time order."""
    sorted_events = (
        pd.Series(event_ids, name="event_id")
        .to_frame()
        .assign(close_time=event_close_times.values)
        .sort_values("close_time")
        .reset_index(drop=True)
    )
    splits = np.array_split(np.arange(len(sorted_events)), n_groups)
    return [sorted_events["event_id"].iloc[s].tolist() for s in splits]


def get_strategy_c_cpcv_splits(
    event_ids: list,
    event_close_times: pd.Series,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_hours: int = EMBARGO_HOURS,
    label_horizon_hours: int = LABEL_HORIZON_HOURS,
) -> Iterator[tuple[list, list]]:
    """
    Yield (train_event_ids, test_event_ids) for each CPCV split.

    Purging removes training events whose close_time overlaps the
    test window (within label_horizon_hours).
    Embargo removes training events within embargo_hours after test_end.

    Total splits = C(n_groups, k_test_groups).
    """
    if embargo_hours < label_horizon_hours:
        raise ValueError(
            f"embargo_hours ({embargo_hours}) must be >= label_horizon_hours "
            f"({label_horizon_hours})"
        )

    event_time_map = dict(zip(event_ids, event_close_times.values))
    groups = _split_events_into_groups(event_ids, event_close_times, n_groups)

    for test_group_indices in combinations(range(n_groups), k_test_groups):
        test_eids = []
        for gi in test_group_indices:
            test_eids.extend(groups[gi])

        test_times = pd.DatetimeIndex([event_time_map[e] for e in test_eids])
        test_start = test_times.min()
        test_end = test_times.max()

        purge_cutoff = test_start - pd.Timedelta(hours=label_horizon_hours)
        embargo_end = test_end + pd.Timedelta(hours=embargo_hours)

        train_eids_raw = []
        for gi in range(n_groups):
            if gi not in test_group_indices:
                train_eids_raw.extend(groups[gi])

        train_eids = [
            e for e in train_eids_raw
            if (event_time_map[e] < purge_cutoff) or (event_time_map[e] > embargo_end)
        ]

        yield train_eids, test_eids


def run_strategy_c_cpcv(
    ladder_df: pd.DataFrame,
    labels_df: pd.DataFrame,
    underlying_bars: pd.DataFrame,
    strategy_c_config: dict,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_hours: int = EMBARGO_HOURS,
    output_dir: str = "backtesting/output/models",
) -> pd.DataFrame:
    """
    Run full event-level CPCV for Strategy C.

    Args:
        ladder_df:          Output of load_strike_ladder_history().
        labels_df:          Output of build_strike_ladder_labels().
        underlying_bars:    1-minute OHLCV bars.
        strategy_c_config:  Contents of strategies/strategy_c/config/{asset}.yaml.
        n_groups:           CPCV groups (default 6).
        k_test_groups:      Test groups per split (default 2).
        embargo_hours:      Post-test embargo window.
        output_dir:         Where to write per-split calibrator artifacts.

    Returns:
        DataFrame with columns:
            split_id, n_train_events, n_test_events, test_start, test_end,
            sharpe, win_rate, brier, log_loss, n_trades,
            c2_violations_detected, c2_violations_traded
    """
    _ensure_strategies_importable()

    # Build event → close_time map
    event_close = (
        labels_df[["event_id", "event_close_time"]]
        .drop_duplicates("event_id")
        .set_index("event_id")["event_close_time"]
    )
    all_event_ids = event_close.index.tolist()

    if len(all_event_ids) < n_groups:
        logger.warning(
            "Only %d events — fewer than n_groups=%d. Cannot run CPCV.",
            len(all_event_ids), n_groups,
        )
        return pd.DataFrame(columns=[
            "split_id", "n_train_events", "n_test_events",
            "test_start", "test_end",
            "sharpe", "win_rate", "brier", "log_loss", "n_trades",
            "c2_violations_detected", "c2_violations_traded",
        ])

    from backtesting.training.strategy_c_fitter import fit_strategy_c
    from backtesting.metrics.calibration import brier_score, log_loss_score
    from backtesting.metrics.trading import sharpe_ratio

    results = []
    split_id = 0

    for train_eids, test_eids in get_strategy_c_cpcv_splits(
        all_event_ids,
        event_close,
        n_groups=n_groups,
        k_test_groups=k_test_groups,
        embargo_hours=embargo_hours,
    ):
        split_id += 1

        if len(train_eids) < 10 or len(test_eids) < 2:
            logger.debug(
                "Split %d: too few events (train=%d, test=%d). Skipping.",
                split_id, len(train_eids), len(test_eids),
            )
            continue

        # Filter data to this split's events
        train_ladder = ladder_df[ladder_df["event_id"].isin(set(train_eids))]
        train_labels = labels_df[labels_df["event_id"].isin(set(train_eids))]
        test_labels = labels_df[labels_df["event_id"].isin(set(test_eids))]

        # Get test time window for logging
        test_times = event_close.loc[event_close.index.isin(test_eids)]
        test_start = test_times.min()
        test_end = test_times.max()

        # Fit calibrators on training split
        split_output_dir = os.path.join(output_dir, f"cpcv_split_{split_id}")
        try:
            fit_result = fit_strategy_c(
                asset=strategy_c_config.get("asset", "btc"),
                underlying_bars=underlying_bars,
                ladder_df=train_ladder,
                labels_df=train_labels,
                config=strategy_c_config,
                output_dir=split_output_dir,
            )
        except Exception as exc:
            logger.warning("Split %d: fit_strategy_c failed: %s", split_id, exc)
            continue

        # Evaluate on test split
        split_metrics = _evaluate_split(
            split_id=split_id,
            test_labels=test_labels,
            test_ladder=ladder_df[ladder_df["event_id"].isin(set(test_eids))],
            underlying_bars=underlying_bars,
            fit_result=fit_result,
            strategy_c_config=strategy_c_config,
            test_start=test_start,
            test_end=test_end,
            n_train_events=len(train_eids),
        )
        results.append(split_metrics)

    return pd.DataFrame(results)


def _evaluate_split(
    split_id: int,
    test_labels: pd.DataFrame,
    test_ladder: pd.DataFrame,
    underlying_bars: pd.DataFrame,
    fit_result: dict,
    strategy_c_config: dict,
    test_start,
    test_end,
    n_train_events: int,
) -> dict:
    """
    Evaluate one CPCV test split. Returns a metrics dict.
    Uses digital_call probability + fitted calibrators; simulates taker fills.
    """
    _ensure_strategies_importable()
    from strategy_c.probability.digital_call import binary_call_probability
    from strategy_c.features.moneyness import compute_moneyness_features
    from backtesting.metrics.calibration import brier_score, log_loss_score
    from backtesting.metrics.trading import sharpe_ratio

    taker_fee = float(
        strategy_c_config.get("fees", {}).get("kalshi", {}).get("taker_fee_rate", 0.03)
    )
    safety_margin = float(strategy_c_config.get("fees", {}).get("safety_margin", 0.005))
    sigma_ref = float(
        strategy_c_config.get("volatility_reference", {}).get("annualized", 0.5)
    )
    rfr = float(strategy_c_config.get("probability", {}).get("risk_free_rate", 0.0))

    bars_sorted = underlying_bars.sort_values("timestamp").reset_index(drop=True)

    y_true_list: list[float] = []
    p_hat_list: list[float] = []
    pnl_list: list[float] = []
    n_trades = 0
    c2_violations_detected = 0
    c2_violations_traded = 0

    # Get earliest snapshot per event for entry price
    if test_ladder.empty:
        pass
    else:
        earliest_snap = (
            test_ladder.sort_values("timestamp")
            .groupby("event_id")["timestamp"]
            .first()
            .rename("entry_time")
        )
        labels_with_entry = test_labels.join(earliest_snap, on="event_id")

        for _, row in labels_with_entry.iterrows():
            entry_time = row.get("entry_time")
            if pd.isna(entry_time):
                continue
            strike = float(row["strike"])
            label = int(row["label"])
            event_close_time = row["event_close_time"]

            candidates = bars_sorted[bars_sorted["timestamp"] <= entry_time]
            if candidates.empty:
                continue
            spot = float(candidates["close"].iloc[-1])
            if spot <= 0:
                continue

            tte_s = (event_close_time - entry_time).total_seconds()
            if tte_s <= 0:
                continue

            t_years = tte_s / (365.25 * 24 * 3600)
            iv = sigma_ref ** 2 * t_years
            p_raw = binary_call_probability(spot, strike, iv, tte_s, rfr)

            feat = compute_moneyness_features(spot, strike, sigma_ref, tte_s, strategy_c_config)
            bucket = feat["moneyness_bucket"]

            # Apply calibrator if available
            cal_paths = fit_result.get("calibrator_paths", {})
            p_cal = _apply_calibrator(p_raw, bucket, cal_paths)

            y_true_list.append(float(label))
            p_hat_list.append(p_cal)

            # Simulate trade: look up market price from ladder snapshot
            event_snaps = test_ladder[test_ladder["event_id"] == row["event_id"]]
            strike_snaps = event_snaps[event_snaps["strike"] == strike]
            if strike_snaps.empty:
                continue

            latest = strike_snaps.sort_values("timestamp").iloc[-1]
            yes_mid = (float(latest["yes_bid"]) + float(latest["yes_ask"])) / 2.0
            p_market = float(np.clip(yes_mid / 100.0 if yes_mid > 1 else yes_mid, 0.01, 0.99))

            edge = p_cal - p_market
            min_edge = taker_fee + safety_margin

            if abs(edge) > min_edge:
                n_trades += 1
                if edge > 0:
                    fill = p_market + taker_fee
                    gross = label - fill
                else:
                    fill = (1.0 - p_market) + taker_fee
                    gross = (1 - label) - fill
                pnl_list.append(gross - taker_fee)

    n_test_events = test_labels["event_id"].nunique()
    pnl_arr = np.array(pnl_list, dtype=float)
    y_arr = np.array(y_true_list, dtype=float)
    p_arr = np.array(p_hat_list, dtype=float)

    return {
        "split_id": split_id,
        "n_train_events": n_train_events,
        "n_test_events": n_test_events,
        "test_start": test_start,
        "test_end": test_end,
        "sharpe": sharpe_ratio(pnl_arr) if len(pnl_arr) > 1 else float("nan"),
        "win_rate": float((pnl_arr > 0).mean()) if len(pnl_arr) > 0 else float("nan"),
        "brier": brier_score(y_arr, p_arr) if len(y_arr) > 0 else float("nan"),
        "log_loss": _safe_log_loss(y_arr, p_arr),
        "n_trades": n_trades,
        "c2_violations_detected": c2_violations_detected,
        "c2_violations_traded": c2_violations_traded,
    }


def _apply_calibrator(p_raw: float, bucket: str, cal_paths: dict) -> float:
    """Load and apply a per-bucket calibrator pickle. Falls back to p_raw."""
    import pickle
    path = cal_paths.get(bucket)
    if not path or not os.path.exists(path):
        return p_raw
    try:
        with open(path, "rb") as f:
            cal = pickle.load(f)
        # IsotonicRegression returns array; LogisticRegression uses predict_proba
        if hasattr(cal, "predict_proba"):
            return float(cal.predict_proba([[p_raw]])[0, 1])
        return float(np.clip(cal.predict([p_raw])[0], 0.0, 1.0))
    except Exception as exc:
        logger.debug("Calibrator load failed for bucket %s: %s", bucket, exc)
        return p_raw


def _safe_log_loss(y_true: np.ndarray, p_hat: np.ndarray) -> float:
    if len(y_true) == 0:
        return float("nan")
    from backtesting.metrics.calibration import log_loss_score
    return log_loss_score(y_true, p_hat)


def _ensure_strategies_importable() -> None:
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)
