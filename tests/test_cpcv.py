import math
import numpy as np
import pandas as pd
import pytest
from backtesting.validation.cpcv import (
    get_cpcv_splits, split_into_groups, _n_combinations, _n_paths,
    LABEL_HORIZON_MINUTES,
)


def _make_timestamps(n: int, freq_min: int = 15) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq=f"{freq_min}min", tz="UTC")


def test_split_count():
    """C(6, 2) = 15 splits."""
    ts = _make_timestamps(180)
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    assert len(splits) == math.comb(6, 2)


def test_path_count_formula():
    """(k · C(N,k)) / N = (2 · 15) / 6 = 5."""
    assert _n_paths(6, 2) == 5


def test_purging_removes_overlapping_labels():
    """
    Training obs at t must be purged if t + label_horizon > test_start.
    Specifically: no training obs in [purge_cutoff, test_start).
    purge_cutoff = test_start - label_horizon_minutes.
    """
    ts = _make_timestamps(180)
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    for train_ts, test_ts in splits:
        if len(train_ts) == 0:
            continue
        test_start = test_ts.min()
        purge_cutoff = test_start - pd.Timedelta(minutes=LABEL_HORIZON_MINUTES)
        # No training timestamp should be in [purge_cutoff, test_start)
        near_test = train_ts[(train_ts >= purge_cutoff) & (train_ts < test_start)]
        assert len(near_test) == 0, (
            f"Unpurged training obs in [{purge_cutoff}, {test_start}): {near_test[:3]}"
        )


def test_embargo_removes_post_test():
    """No training obs in (test_end, embargo_end]."""
    ts = _make_timestamps(180)
    embargo_min = 45
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=embargo_min))
    for train_ts, test_ts in splits:
        if len(train_ts) == 0:
            continue
        test_end = test_ts.max()
        embargo_end = test_end + pd.Timedelta(minutes=embargo_min)
        in_embargo = train_ts[(train_ts > test_end) & (train_ts <= embargo_end)]
        assert len(in_embargo) == 0, (
            f"Training obs in embargo window ({test_end}, {embargo_end}]: {in_embargo[:3]}"
        )


def test_train_test_disjoint():
    ts = _make_timestamps(180)
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    for train_ts, test_ts in splits:
        overlap = set(train_ts) & set(test_ts)
        assert len(overlap) == 0, f"Train/test overlap: {len(overlap)} timestamps"


def test_embargo_floor_enforced():
    ts = _make_timestamps(60)
    with pytest.raises(ValueError, match="embargo_minutes"):
        list(get_cpcv_splits(ts, embargo_minutes=10))  # < 15 min floor
