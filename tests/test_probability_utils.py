import numpy as np
import pytest
from shared.probability_utils import (
    drift_vol_to_prob,
    prob_to_contract_price,
    contract_price_change_from_prob_change,
)


def test_zero_drift_gives_half():
    p = drift_vol_to_prob(mu=0.0, sigma=0.5, dt=1/365)
    assert abs(p - 0.5) < 1e-10


def test_positive_drift_above_half():
    p = drift_vol_to_prob(mu=100.0, sigma=0.5, dt=1/(365*96))
    assert p > 0.5


def test_negative_drift_below_half():
    p = drift_vol_to_prob(mu=-100.0, sigma=0.5, dt=1/(365*96))
    assert p < 0.5


def test_zero_sigma_returns_half():
    assert drift_vol_to_prob(mu=1.0, sigma=0.0, dt=1/365) == 0.5


def test_zero_dt_returns_half():
    assert drift_vol_to_prob(mu=1.0, sigma=0.5, dt=0.0) == 0.5


def test_prob_to_price():
    assert prob_to_contract_price(0.7) == pytest.approx(70.0)
    assert prob_to_contract_price(0.0) == 0.0
    assert prob_to_contract_price(1.0) == 100.0


def test_dp_to_dc():
    assert contract_price_change_from_prob_change(0.05) == pytest.approx(5.0)
    assert contract_price_change_from_prob_change(-0.03) == pytest.approx(-3.0)


def test_realistic_15min_value():
    dt = 15.0 / (365 * 24 * 60)
    p = drift_vol_to_prob(mu=1.0, sigma=0.8, dt=dt)
    # Verify formula: Φ(1.0 * sqrt(dt) / 0.8)
    from scipy.stats import norm
    expected = float(norm.cdf(1.0 * (dt ** 0.5) / 0.8))
    assert p == pytest.approx(expected, rel=1e-6)
