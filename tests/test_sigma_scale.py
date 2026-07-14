"""fit_sigma_scale: recovers a known vol miscalibration from (z, outcome) pairs."""
import sys, os, random
from statistics import NormalDist

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts.calibration import fit_sigma_scale, MIN_SIGMA_SAMPLES, SIGMA_SCALE_LO, SIGMA_SCALE_HI

ND = NormalDist()


def _rows(s_true, n=2000, seed=7):
    """Outcomes drawn from Phi(z/s_true): the logged sigma was 1/s_true of the truth."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        z = rng.uniform(-2.5, 2.5)
        p = ND.cdf(z / s_true)
        rows.append((z, "yes" if rng.random() < p else "no"))
    return rows


def test_recovers_deflation_for_inflated_sigma():
    # An inflated logged sigma shrinks every |z|, so the truth is Phi(z/s) with s < 1
    # and the fitted scale shrinks sigma back down. s_true=0.5 = "sigma was 2x too big".
    got = fit_sigma_scale(_rows(0.5))
    assert 0.4 <= got <= 0.65, f"expected ~0.5, got {got}"


def test_recovers_inflation_for_overconfident_model():
    got = fit_sigma_scale(_rows(1.6))
    assert 1.4 <= got <= 1.85, f"expected ~1.6, got {got}"


def test_neutral_when_calibrated():
    got = fit_sigma_scale(_rows(1.0))
    assert 0.85 <= got <= 1.15, f"expected ~1.0, got {got}"


def test_neutral_below_sample_gate():
    assert fit_sigma_scale(_rows(0.5)[: MIN_SIGMA_SAMPLES - 1]) == 1.0


def test_clamped_to_bounds():
    got = fit_sigma_scale(_rows(0.2))
    assert got >= SIGMA_SCALE_LO
    got = fit_sigma_scale(_rows(5.0))
    assert got <= SIGMA_SCALE_HI


def test_ignores_junk_rows():
    rows = _rows(1.0) + [(None, "yes"), (float("nan"), "no"), (1.0, "pending"), ("x", "yes")]
    got = fit_sigma_scale(rows)
    assert 0.85 <= got <= 1.15
