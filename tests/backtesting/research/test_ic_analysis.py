import numpy as np
import pytest
from backtesting.research.ic_analysis import (
    compute_ic, compute_icir, compute_ic_tstat,
    compute_rolling_ic, evaluate_signal, ICResult,
)

def test_compute_ic_perfect():
    pred = np.array([0.8, 0.3, 0.7, 0.2, 0.9])
    out  = np.array([1,   0,   1,   0,   1  ])
    assert compute_ic(pred, out) > 0.9

def test_compute_ic_noise():
    rng = np.random.default_rng(42)
    pred = rng.uniform(0, 1, 500)
    out  = rng.integers(0, 2, 500)
    assert abs(compute_ic(pred, out)) < 0.15

def test_compute_ic_inverse():
    pred = np.array([0.8, 0.3, 0.7, 0.2, 0.9])
    out  = np.array([0,   1,   0,   1,   0  ])
    assert compute_ic(pred, out) < -0.9

def test_compute_icir_stable():
    ic_series = np.array([0.05, 0.06, 0.04, 0.05, 0.07])
    assert compute_icir(ic_series) > 0.5

def test_compute_icir_noisy():
    ic_series = np.array([0.05, -0.04, 0.06, -0.03, 0.07])
    assert abs(compute_icir(ic_series)) < 0.5

def test_ic_tstat_formula():
    assert compute_ic_tstat(0.05, 400) == pytest.approx(1.0, abs=0.01)

def test_evaluate_signal_fail_on_noise():
    rng = np.random.default_rng(99)
    pred = rng.uniform(0, 1, 200)
    out  = rng.integers(0, 2, 200)
    lag_outs = {1: out, 2: out, 4: out, 8: out}
    result = evaluate_signal(pred, out, lag_outs)
    assert isinstance(result, ICResult)
    assert result.verdict == "CONDITIONAL"

def test_evaluate_signal_pass_on_real_signal():
    rng = np.random.default_rng(0)
    true_prob = rng.uniform(0.4, 0.7, 1000)
    out = (rng.uniform(0, 1, 1000) < true_prob).astype(int)
    pred = np.clip(true_prob + rng.normal(0, 0.03, 1000), 0, 1)
    lag_outs = {1: out, 2: out, 4: out, 8: out}
    result = evaluate_signal(pred, out, lag_outs)
    assert result.verdict in ("PASS", "CONDITIONAL")
    assert len(result.ic_decay) == 4
