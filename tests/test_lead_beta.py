"""fit_lead_beta recovers the lag-1 lead coefficient that fit_rolling_beta cannot."""
import sys, os, math, random, time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts.calibration import fit_lead_beta, fit_rolling_beta, MIN_BETA_PAIRS


def _series(lead_beta=0.4, n=60, step=30.0, seed=11):
    """BTC random-walk grid prices; alt whose return at i follows BTC's return at i-1."""
    rng = random.Random(seed)
    now = time.time()
    t0 = now - n * step
    btc, alt = 60000.0, 150.0
    btc_pts, alt_pts = [(t0, btc)], [(t0, alt)]
    x_prev = 0.0
    for i in range(1, n + 1):
        x = rng.gauss(0.0, 0.0012)          # BTC 30s return
        y = lead_beta * x_prev              # alt follows the PRIOR BTC return, no noise
        btc *= math.exp(x)
        alt *= math.exp(y)
        t = t0 + i * step
        btc_pts.append((t, btc))
        alt_pts.append((t, alt))
        x_prev = x
    return btc_pts, alt_pts


def test_lead_beta_recovers_known_lag_coefficient():
    btc_pts, alt_pts = _series(lead_beta=0.4)
    beta, n = fit_lead_beta(btc_pts, alt_pts)
    assert n >= MIN_BETA_PAIRS
    assert beta == pytest.approx(0.4, abs=0.05)


def test_rolling_beta_misses_the_lag_relationship():
    # The contemporaneous fit sees ~zero on lag-1 data: the two estimators measure
    # different quantities, which is exactly why S1 must use the lead fit.
    btc_pts, alt_pts = _series(lead_beta=0.4)
    beta, n = fit_rolling_beta(btc_pts, alt_pts)
    if beta is not None:
        assert abs(beta - 0.4) > 0.2, f"contemporaneous fit should not see the lag: {beta}"


def test_lead_beta_thin_data_gate():
    btc_pts, alt_pts = _series(n=10)
    beta, n = fit_lead_beta(btc_pts, alt_pts)
    assert beta is None
    assert n < MIN_BETA_PAIRS
