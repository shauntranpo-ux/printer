"""
scripts/backtest_signals.py helpers: no-lookahead spot lookup, trailing sigma,
and the per-window decision-row builder - all against synthetic candles, since the
container can't fetch real ones.
"""
import sys
import os
import math
from datetime import datetime, timedelta, timezone

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

import backtest_signals as bt

_T0 = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())


def _closes(n=200, base=100.0, step=0.001):
    """Deterministic tape: alternating +/-step 1-min log returns from _T0."""
    out, p = {}, base
    for i in range(n):
        p *= math.exp(step if i % 2 else -step)
        out[_T0 + 60 * i] = p
    return out


def test_spot_at_never_uses_the_forming_candle():
    closes = _closes()
    ep = _T0 + 60 * 100
    # The candle starting at ep covers [ep, ep+60) and is still forming at ep.
    assert bt.spot_at(closes, ep) == closes[ep - 60]
    assert bt.spot_at(closes, ep) != closes[ep]
    # Gap of 3 missing minutes: walks back to the last completed candle.
    gappy = dict(closes)
    for k in (1, 2, 3):
        del gappy[ep - 60 * k]
    assert bt.spot_at(gappy, ep) == closes[ep - 240]
    # Nothing within 5 minutes -> None.
    assert bt.spot_at({}, ep) is None


def test_sigma15_matches_constant_return_tape():
    closes = _closes(step=0.001)
    sig = bt.sigma15_at(closes, _T0 + 60 * 150)
    # |r| = 0.001 every minute -> var/min = 1e-6 -> sigma15 = 0.001 * sqrt(15).
    assert sig == pytest.approx(0.001 * math.sqrt(15), rel=1e-6)
    # Fewer than _MIN_VOL_RETURNS valid pairs -> None.
    assert bt.sigma15_at(_closes(n=20), _T0 + 60 * 20) is None


def test_build_rows_z_and_momentum_signs():
    closes = _closes(step=0.001)
    w_close = datetime.fromtimestamp(_T0 + 60 * 150, tz=timezone.utc)
    settlements = pd.DataFrame({
        "window_open": [w_close - timedelta(minutes=15)],
        "close_time": [w_close],
        "strike": [90.0],     # spot ~100 -> deep YES favorite
        "result": [1],
    })
    rows = bt.build_rows(closes, settlements)
    assert set(rows["mins"]) == {10, 7, 5, 3}
    assert (rows["z"] > 0).all() and (rows["p_yes"] > 0.99).all()
    assert rows["mom_z"].notna().all() and rows["run_z"].notna().all()
    # Strike above spot flips the sign.
    settlements["strike"] = 110.0
    flipped = bt.build_rows(closes, settlements)
    assert (flipped["z"] < 0).all()


def test_build_rows_skips_windows_without_candles():
    closes = _closes()
    w_close = datetime.fromtimestamp(_T0 + 60 * 5000, tz=timezone.utc)  # off the tape
    settlements = pd.DataFrame({
        "window_open": [w_close - timedelta(minutes=15)],
        "close_time": [w_close], "strike": [100.0], "result": [0],
    })
    assert len(bt.build_rows(closes, settlements)) == 0


def test_wilson_lb_bounds():
    assert bt.wilson_lb(0, 0) == 0.0
    assert 0.51 < bt.wilson_lb(5600, 10000) < 0.56
    assert bt.wilson_lb(560, 1000) < bt.wilson_lb(5600, 10000)  # tighter with more n


def test_load_closes_survives_a_real_parquet_round_trip(tmp_path):
    """Regression: load_closes() must derive epoch seconds in a unit-agnostic way.
    A raw `.astype("int64") // 10**9` conversion silently assumes the column is
    nanosecond-resolution - true pre-pandas-3.0 (read_parquet always upcast to
    ns), but pandas >=3.0 preserves the parquet file's native unit (commonly
    microseconds from pyarrow's default Timestamp precision), which made every
    epoch key wrong by 1000x and silently zeroed out every decision row. The
    synthetic-dict tests above never exercise this path - only a real
    to_parquet/read_parquet round-trip catches a unit regression."""
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    df = pd.DataFrame({
        "ts": [t0, t0 + timedelta(minutes=1), t0 + timedelta(minutes=2)],
        "open": [100.0, 101.0, 102.0], "high": [100.0, 101.0, 102.0],
        "low": [100.0, 101.0, 102.0], "close": [100.0, 101.0, 102.0],
        "volume": [1.0, 1.0, 1.0],
    })
    path = str(tmp_path / "candles.parquet")
    df.to_parquet(path, index=False)
    closes = bt.load_closes(path)
    expected_first = int(t0.timestamp())
    assert expected_first in closes, (
        f"epoch key {expected_first} missing - got keys like {sorted(closes)[:3]} "
        "(a 1000x-off bug produces keys near the 1970 epoch)")
    assert closes[expected_first] == 100.0
    assert closes[expected_first + 60] == 101.0
    assert closes[expected_first + 120] == 102.0
