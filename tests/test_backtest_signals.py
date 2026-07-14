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
