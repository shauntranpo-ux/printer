import numpy as np
import pandas as pd
import pytest
from strategy_a.features.time_of_day import compute, _SESSIONS, _DAYS

_EXPECTED_KEYS = (
    {f"session_{name}" for name, _, _ in _SESSIONS}
    | {f"dow_{d}" for d in _DAYS}
    | {"is_weekend", "minute_sin", "minute_cos",
       "minutes_until_0800", "minutes_until_1430", "monday_asia_open"}
)


def test_smoke():
    assert isinstance(compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC")), dict)


def test_shape():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert _EXPECTED_KEYS.issubset(result.keys())


def test_session_one_hot_sum():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    session_values = [v for k, v in result.items() if k.startswith("session_")]
    assert sum(session_values) == 1.0


def test_eu_open_active_at_10h():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["session_eu_open"] == 1.0


def test_weekend_flag():
    # 2026-04-25 is Saturday
    result = compute(pd.Timestamp("2026-04-25 12:00:00", tz="UTC"))
    assert result["is_weekend"] == 1.0


def test_weekday_not_weekend():
    # 2026-04-22 is Wednesday
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["is_weekend"] == 0.0


def test_cyclic_range():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert -1.0 <= result["minute_sin"] <= 1.0
    assert -1.0 <= result["minute_cos"] <= 1.0


def test_proximity_clipped_to_120():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert 0.0 <= result["minutes_until_0800"] <= 120.0
    assert 0.0 <= result["minutes_until_1430"] <= 120.0


def test_monday_asia_open_true_mon_2h():
    # Monday 02:00 UTC
    result = compute(pd.Timestamp("2026-04-27 02:00:00", tz="UTC"))
    assert result["monday_asia_open"] == 1.0


def test_monday_asia_open_false_mid_week():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["monday_asia_open"] == 0.0


def test_dow_one_hot_sum():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    dow_values = [v for k, v in result.items() if k.startswith("dow_")]
    assert sum(dow_values) == 1.0
