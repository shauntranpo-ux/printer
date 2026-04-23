"""Tests for backtesting/validation/strategy_c_cpcv_adapter.py."""
import sys
import os

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.validation.strategy_c_cpcv_adapter import (
    get_strategy_c_cpcv_splits,
    _split_events_into_groups,
)


def _make_events(n: int = 18) -> tuple[list, pd.Series]:
    start = pd.Timestamp("2024-01-01 01:00:00", tz="UTC")
    event_ids = [f"ev_{i:04d}" for i in range(n)]
    close_times = pd.Series(
        [start + pd.Timedelta(hours=i) for i in range(n)],
        name="event_close_time",
    )
    return event_ids, close_times


class TestSplitEventsIntoGroups:
    def test_returns_n_groups(self):
        eids, cts = _make_events(18)
        groups = _split_events_into_groups(eids, cts, n_groups=6)
        assert len(groups) == 6

    def test_all_events_covered(self):
        eids, cts = _make_events(18)
        groups = _split_events_into_groups(eids, cts, n_groups=6)
        all_ids = [e for g in groups for e in g]
        assert sorted(all_ids) == sorted(eids)

    def test_no_duplicates(self):
        eids, cts = _make_events(18)
        groups = _split_events_into_groups(eids, cts, n_groups=6)
        all_ids = [e for g in groups for e in g]
        assert len(all_ids) == len(set(all_ids))


class TestGetStrategyCCpcvSplits:
    def test_yields_correct_number_of_splits(self):
        eids, cts = _make_events(18)
        splits = list(get_strategy_c_cpcv_splits(eids, cts, n_groups=6, k_test_groups=2))
        import math
        assert len(splits) == math.comb(6, 2)

    def test_each_split_has_train_and_test(self):
        eids, cts = _make_events(18)
        for train, test in get_strategy_c_cpcv_splits(eids, cts):
            assert isinstance(train, list)
            assert isinstance(test, list)

    def test_train_and_test_disjoint(self):
        eids, cts = _make_events(18)
        for train, test in get_strategy_c_cpcv_splits(eids, cts):
            assert set(train).isdisjoint(set(test))

    def test_purge_removes_events_too_close_to_test(self):
        eids, cts = _make_events(18)
        for train, test in get_strategy_c_cpcv_splits(eids, cts):
            if not train or not test:
                continue
            test_times = cts[cts.index.isin([eids.index(e) for e in test if e in eids])]
            # training events must not be within 1h of test_start
            test_idx = [eids.index(e) for e in test]
            test_times_s = pd.Series([cts.iloc[i] for i in test_idx])
            test_start = test_times_s.min()
            purge_cutoff = test_start - pd.Timedelta(hours=1)
            for e in train:
                t = cts.iloc[eids.index(e)]
                # Either before purge cutoff or after embargo end
                assert t < purge_cutoff or t > test_times_s.max() + pd.Timedelta(hours=1)

    def test_raises_if_embargo_less_than_horizon(self):
        eids, cts = _make_events(18)
        with pytest.raises(ValueError):
            list(get_strategy_c_cpcv_splits(eids, cts, embargo_hours=0, label_horizon_hours=1))

    def test_handles_small_event_set(self):
        eids, cts = _make_events(6)
        splits = list(get_strategy_c_cpcv_splits(eids, cts, n_groups=6, k_test_groups=2))
        # Even with 1 event per group, should still yield splits
        assert len(splits) > 0
