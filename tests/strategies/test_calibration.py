import pytest

from strategies.calibration import AssetCalibrator, MIN_SAMPLES_FOR_FIT
import strategies.calibration as calib_module


def test_identity_before_fit():
    c = AssetCalibrator("BTC")
    for p in [0.1, 0.5, 0.9]:
        assert c.calibrate(p) == p


def test_refit_below_threshold_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(calib_module, "CALIBRATION_DIR", tmp_path)
    c = AssetCalibrator("TEST")
    c.refit([0.5] * 10, [1] * 10)  # only 10 samples, below 50
    assert c.model is None


def test_refit_correcting_overconfident_model(tmp_path, monkeypatch):
    monkeypatch.setattr(calib_module, "CALIBRATION_DIR", tmp_path)
    c = AssetCalibrator("TEST")
    # Model says 0.9 for 100 trades, but only 60% actually win
    raw = [0.9] * 100
    outcomes = [1] * 60 + [0] * 40
    c.refit(raw, outcomes)
    # After calibration, 0.9 raw should map to ~0.6
    calibrated = c.calibrate(0.9)
    assert 0.55 <= calibrated <= 0.65


def test_refit_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(calib_module, "CALIBRATION_DIR", tmp_path)
    c1 = AssetCalibrator("TEST")
    raw = [0.9] * 100
    outcomes = [1] * 60 + [0] * 40
    c1.refit(raw, outcomes)
    calibrated_before = c1.calibrate(0.9)

    # New instance loads the saved model
    c2 = AssetCalibrator("TEST")
    calibrated_after = c2.calibrate(0.9)
    assert abs(calibrated_before - calibrated_after) < 0.01
