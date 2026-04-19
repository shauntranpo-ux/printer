import pytest
from strategies.signals.taper import magnitude_taper


def test_taper_at_midpoint_is_one():
    assert magnitude_taper(0.5) == pytest.approx(1.0, abs=1e-9)


def test_taper_at_zero_is_zero():
    assert magnitude_taper(0.0) == pytest.approx(0.0, abs=1e-9)


def test_taper_at_one_is_zero():
    assert magnitude_taper(1.0) == pytest.approx(0.0, abs=1e-9)


def test_taper_monotone_toward_midpoint():
    assert magnitude_taper(0.1) < magnitude_taper(0.25) < magnitude_taper(0.5)
    assert magnitude_taper(0.9) < magnitude_taper(0.75) < magnitude_taper(0.5)


def test_taper_clamps_out_of_range():
    assert magnitude_taper(-0.5) == pytest.approx(magnitude_taper(0.0), abs=1e-9)
    assert magnitude_taper(1.5) == pytest.approx(magnitude_taper(1.0), abs=1e-9)
