import numpy as np
import pytest
from backtesting.metrics.calibration import (
    brier_score, log_loss_score, expected_calibration_error,
    reliability_diagram_data,
)


def test_brier_perfect():
    y = np.array([1, 0, 1, 0])
    p = np.array([1.0, 0.0, 1.0, 0.0])
    assert brier_score(y, p) == pytest.approx(0.0)


def test_brier_constant_half_balanced():
    """Constant p=0.5 on balanced labels → Brier = 0.25."""
    y = np.array([1, 0, 1, 0, 1, 0])
    p = np.full(6, 0.5)
    assert brier_score(y, p) == pytest.approx(0.25)


def test_brier_worst_case():
    y = np.array([1, 1, 0, 0])
    p = np.array([0.0, 0.0, 1.0, 1.0])
    assert brier_score(y, p) == pytest.approx(1.0)


def test_log_loss_perfect():
    y = np.array([1, 0])
    p = np.array([0.9999999, 0.0000001])
    assert log_loss_score(y, p) < 0.001


def test_ece_perfect_calibration():
    """Perfectly calibrated predictions → ECE near zero."""
    rng = np.random.default_rng(42)
    p = rng.uniform(0, 1, 1000)
    y = (rng.random(1000) < p).astype(int)
    ece = expected_calibration_error(y, p, n_bins=10)
    assert ece < 0.05


def test_ece_constant_predictions():
    """Constant p=0.5 on balanced labels → ECE ≈ 0."""
    y = np.array([1, 0] * 500)
    p = np.full(1000, 0.5)
    ece = expected_calibration_error(y, p, n_bins=10)
    assert ece < 0.01


def test_reliability_diagram_shape():
    y = np.array([1, 0, 1, 0, 1, 0] * 20)
    p = np.random.default_rng(0).uniform(0, 1, 120)
    df = reliability_diagram_data(y, p, n_bins=5)
    assert len(df) == 5
    assert "fraction_positive" in df.columns
    assert "count" in df.columns
