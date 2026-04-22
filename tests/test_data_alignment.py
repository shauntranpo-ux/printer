import pandas as pd
import pytest
from backtesting.data.aligner import build_event_stream, iter_windows


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _df(timestamps, **cols):
    d = {"timestamp": [_ts(t) for t in timestamps]}
    d.update(cols)
    return pd.DataFrame(d)


def test_event_stream_sorted():
    bars = _df(["2025-01-01 00:10:00", "2025-01-01 00:00:00"],
               open=[1.0, 2.0], close=[1.1, 2.1], high=[1.2, 2.2], low=[0.9, 1.9], volume=[10.0, 20.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[50000.0],
                 reference_price_close=[50100.0], log_return=[0.002])
    events = build_event_stream(bars, labels)
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)


def test_event_types_present():
    bars = _df(["2025-01-01 00:00:00"], open=[1.0], close=[1.1], high=[1.2], low=[0.9], volume=[5.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[50000.0],
                 reference_price_close=[50100.0], log_return=[0.002])
    events = build_event_stream(bars, labels)
    types = {e.event_type for e in events}
    assert "bar" in types
    assert "label" in types


def test_timezone_naive_raises():
    bars_naive = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01 00:00:00"]),
        "open": [1.0], "close": [1.1], "high": [1.2], "low": [0.9], "volume": [5.0]
    })
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[1.0],
                 reference_price_close=[1.1], log_return=[0.001])
    with pytest.raises(ValueError, match="UTC-aware"):
        build_event_stream(bars_naive, labels)


def test_iter_windows_no_lookahead():
    bars = _df([
        "2025-01-01 00:00:00",
        "2025-01-01 00:05:00",
        "2025-01-01 00:10:00",
        "2025-01-01 00:20:00",
    ], open=[1.0]*4, close=[1.1]*4, high=[1.2]*4, low=[0.9]*4, volume=[5.0]*4)
    labels = _df([
        "2025-01-01 00:15:00",
        "2025-01-01 00:30:00",
    ], label=[1, 0], reference_price_open=[1.0, 1.1],
       reference_price_close=[1.1, 1.05], log_return=[0.001, -0.001])
    events = build_event_stream(bars, labels)
    label_index = pd.DatetimeIndex([_ts("2025-01-01 00:15:00"), _ts("2025-01-01 00:30:00")])
    windows = list(iter_windows(events, label_index))

    ts1, evts1 = windows[0]
    bar_times = [e.timestamp for e in evts1 if e.event_type == "bar"]
    assert all(t < ts1 for t in bar_times)

    ts2, evts2 = windows[1]
    bar_times2 = [e.timestamp for e in evts2 if e.event_type == "bar"]
    assert all(t < ts2 for t in bar_times2)
    assert _ts("2025-01-01 00:20:00") in bar_times2


def test_none_inputs_skipped():
    bars = _df(["2025-01-01 00:00:00"], open=[1.0], close=[1.1], high=[1.2], low=[0.9], volume=[5.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[1.0],
                 reference_price_close=[1.1], log_return=[0.001])
    events = build_event_stream(bars, labels, l2_snapshots=None, trades=None)
    types = {e.event_type for e in events}
    assert "l2" not in types
    assert "trade" not in types
