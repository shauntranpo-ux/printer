"""Tests for AssetCalibrator Platt/isotonic/identity auto-selection."""

import pytest


def test_calibrator_identity_when_too_few_samples():
    """refit() with < 15 samples leaves calibrator in identity mode."""
    from strategies.calibration import AssetCalibrator
    cal = AssetCalibrator.__new__(AssetCalibrator)
    cal.asset = "TEST"
    cal._method = None
    cal._isotonic = None
    cal._platt = None
    cal.sample_count = 0

    raw_probs = [0.6, 0.7, 0.8]
    outcomes = [1, 1, 0]
    cal.refit(raw_probs, outcomes)

    assert cal._method is None
    assert cal.calibrate(0.7) == 0.7  # identity pass-through


def test_calibrator_uses_platt_for_small_samples(tmp_path, monkeypatch):
    """refit() with 15-49 samples selects Platt scaling."""
    from strategies import calibration as cal_mod
    monkeypatch.setattr(cal_mod, "CALIBRATION_DIR", tmp_path)

    from strategies.calibration import AssetCalibrator
    cal = AssetCalibrator("PLATT_TEST")

    import random
    rng = random.Random(42)
    n = 20
    raw_probs = [rng.uniform(0.4, 0.9) for _ in range(n)]
    outcomes = [1 if p > 0.6 else 0 for p in raw_probs]

    cal.refit(raw_probs, outcomes)

    assert cal._method == "platt", f"expected platt, got {cal._method}"
    assert cal._platt is not None
    result = cal.calibrate(0.7)
    assert 0.0 < result < 1.0


def test_calibrator_uses_isotonic_for_large_samples(tmp_path, monkeypatch):
    """refit() with >= 50 samples selects isotonic regression."""
    from strategies import calibration as cal_mod
    monkeypatch.setattr(cal_mod, "CALIBRATION_DIR", tmp_path)

    from strategies.calibration import AssetCalibrator
    cal = AssetCalibrator("ISO_TEST")

    import random
    rng = random.Random(42)
    n = 60
    raw_probs = [rng.uniform(0.4, 0.9) for _ in range(n)]
    outcomes = [1 if p > 0.6 else 0 for p in raw_probs]

    cal.refit(raw_probs, outcomes)

    assert cal._method == "isotonic", f"expected isotonic, got {cal._method}"
    assert cal._isotonic is not None
    result = cal.calibrate(0.7)
    assert 0.0 < result < 1.0


def test_calibrator_platt_persists_and_reloads(tmp_path, monkeypatch):
    """Platt model survives serialization/deserialization."""
    from strategies import calibration as cal_mod
    monkeypatch.setattr(cal_mod, "CALIBRATION_DIR", tmp_path)

    from strategies.calibration import AssetCalibrator
    import random
    rng = random.Random(7)
    n = 25
    raw_probs = [rng.uniform(0.3, 0.9) for _ in range(n)]
    outcomes = [1 if p > 0.55 else 0 for p in raw_probs]

    cal1 = AssetCalibrator("PERSIST_TEST")
    cal1.refit(raw_probs, outcomes)
    p_before = cal1.calibrate(0.65)

    cal2 = AssetCalibrator("PERSIST_TEST")
    assert cal2._method == "platt"
    p_after = cal2.calibrate(0.65)

    assert abs(p_before - p_after) < 1e-6, "Platt model must survive reload"


def test_calibrator_monotone(tmp_path, monkeypatch):
    """calibrate() must be non-decreasing: higher raw_p → higher calibrated_p."""
    from strategies import calibration as cal_mod
    monkeypatch.setattr(cal_mod, "CALIBRATION_DIR", tmp_path)

    from strategies.calibration import AssetCalibrator
    import random
    rng = random.Random(99)
    n = 60
    raw_probs = [rng.uniform(0.2, 0.9) for _ in range(n)]
    outcomes = [1 if p > 0.55 else 0 for p in raw_probs]

    cal = AssetCalibrator("MONO_TEST")
    cal.refit(raw_probs, outcomes)

    probe_points = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    calibrated = [cal.calibrate(p) for p in probe_points]
    for i in range(len(calibrated) - 1):
        assert calibrated[i] <= calibrated[i + 1] + 1e-6, (
            f"Calibration not monotone: calibrate({probe_points[i]})={calibrated[i]:.4f} "
            f"> calibrate({probe_points[i+1]})={calibrated[i+1]:.4f}"
        )
