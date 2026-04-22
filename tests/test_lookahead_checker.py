import pandas as pd
import pytest
from backtesting.validation.lookahead_checker import check_no_lookahead, assert_no_lookahead


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def test_clean_features_no_violations():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "har_rv": _ts("2025-01-01 00:59:50"),
            "ofi": _ts("2025-01-01 00:59:55"),
        }
    }]
    assert check_no_lookahead(decisions) == []


def test_future_feature_detected():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "bad_feature": _ts("2025-01-01 01:00:01"),
        }
    }]
    violations = check_no_lookahead(decisions)
    assert len(violations) == 1
    assert violations[0].feature_name == "bad_feature"
    assert violations[0].delta_seconds == 1.0


def test_same_timestamp_is_violation():
    """t_source == t_decision is still a look-ahead (must be STRICTLY before)."""
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "exact_match": _ts("2025-01-01 01:00:00"),
        }
    }]
    violations = check_no_lookahead(decisions)
    assert len(violations) == 1


def test_assert_raises_on_violation():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "future_data": _ts("2025-01-01 01:05:00"),
        }
    }]
    with pytest.raises(RuntimeError, match="LOOK-AHEAD DETECTED"):
        assert_no_lookahead(decisions)


def test_empty_decisions_passes():
    assert check_no_lookahead([]) == []
