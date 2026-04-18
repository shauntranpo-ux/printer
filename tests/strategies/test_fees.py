import pytest
from strategies.fees import taker_fee, maker_fee, fee_as_pct_of_stake


def test_taker_fee_at_fifty_cents():
    # 10 contracts at $0.50: 0.07 * 10 * 0.5 * 0.5 = 0.175 -> ceil = $0.18
    assert taker_fee(10, 0.50) == 0.18


def test_maker_fee_at_fifty_cents():
    # 10 contracts at $0.50: 0.0175 * 10 * 0.5 * 0.5 = 0.04375 -> ceil = $0.05
    assert maker_fee(10, 0.50) == 0.05


def test_taker_maker_ratio_approx_four_to_one():
    t = taker_fee(100, 0.50)
    m = maker_fee(100, 0.50)
    # Before ceiling: 1.75 vs 0.4375 = 4.0 ratio
    # After ceiling: may not be exactly 4.0 but close
    assert 3.5 <= (t / m) <= 4.5


def test_fee_peaks_at_fifty_cents():
    f_mid = taker_fee(100, 0.50)
    f_low = taker_fee(100, 0.20)
    f_high = taker_fee(100, 0.80)
    assert f_mid > f_low
    assert f_mid > f_high


def test_fee_symmetry_around_fifty():
    # p * (1-p) is theoretically symmetric around 0.5, but IEEE 754 arithmetic
    # means 1.0 - 0.70 != 0.30 exactly, which can shift the ceil by 1 cent.
    # Assert symmetry to within 1 cent (the minimum rounding unit).
    assert round(abs(taker_fee(100, 0.30) - taker_fee(100, 0.70)), 2) <= 0.01


def test_zero_contracts_zero_fee():
    assert taker_fee(0, 0.50) == 0.0
    assert maker_fee(0, 0.50) == 0.0


def test_fee_at_extreme_prices_small():
    # Near 1c or 99c, fees should round up to 1 cent minimum
    assert taker_fee(1, 0.01) == 0.01  # 0.07 * 1 * 0.01 * 0.99 = 0.000693 -> 0.01
    assert taker_fee(1, 0.99) == 0.01


def test_fee_as_pct_at_fifty_cents_around_three_six_pct():
    # 10 contracts @ $0.50, stake = $5, fee = $0.18
    # pct = 0.18 / 5.00 = 0.036 = 3.6%
    pct = fee_as_pct_of_stake(10, 0.50, taker=True)
    assert 0.035 <= pct <= 0.04
