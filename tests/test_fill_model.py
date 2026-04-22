"""
Tests for backtesting/simulation/fill_model.py

TDD: these tests are written before the implementation exists.
"""
import math
import numpy as np
import pandas as pd
import pytest

from backtesting.simulation.fill_model import TakerFillModel, MakerFillModel


def _tick(ts, yes_bid=45, yes_ask=55, no_bid=45, no_ask=55):
    return {
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
    }


# ── TakerFillModel ────────────────────────────────────────────────────────────


class TestTakerFillModelEmpty:
    def test_empty_ticks_returns_none(self):
        model = TakerFillModel()
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [])
        assert result is None


class TestTakerFillModelSpreadCrossing:
    def test_yes_side_fills_at_yes_ask(self):
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01 00:00:00", yes_bid=40, yes_ask=60)
        sig_ts = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
        result = model.fill("yes", sig_ts, [tick])
        assert result is not None
        # yes_ask = 60 cents → 0.60
        assert result["fill_price"] == pytest.approx(0.60)

    def test_no_side_fills_at_no_ask(self):
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01 00:00:00", no_bid=35, no_ask=65)
        sig_ts = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
        result = model.fill("no", sig_ts, [tick])
        assert result is not None
        # no_ask = 65 cents → 0.65
        assert result["fill_price"] == pytest.approx(0.65)


class TestTakerFillModelLatency:
    def test_uses_tick_at_or_after_signal_plus_latency(self):
        """With 500ms latency, should use the first tick >= signal_ts + 500ms."""
        model = TakerFillModel(latency_ms=500.0)
        base = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
        ticks = [
            _tick("2024-01-01 00:00:00", yes_ask=50),   # before target
            _tick("2024-01-01 00:00:00.400000", yes_ask=55),  # before target (400ms)
            _tick("2024-01-01 00:00:00.500000", yes_ask=60),  # exactly at target
            _tick("2024-01-01 00:00:01", yes_ask=70),   # after target
        ]
        result = model.fill("yes", base, ticks)
        assert result is not None
        # first tick at or after base + 500ms → yes_ask=60 → 0.60
        assert result["fill_price"] == pytest.approx(0.60)

    def test_fallback_to_last_tick_when_none_after_latency(self):
        """When no tick is >= signal_ts + latency, fall back to last tick."""
        model = TakerFillModel(latency_ms=5000.0)  # 5s latency
        base = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
        ticks = [
            _tick("2024-01-01 00:00:00", yes_ask=40),
            _tick("2024-01-01 00:00:01", yes_ask=50),  # all before 5s target
        ]
        result = model.fill("yes", base, ticks)
        assert result is not None
        # fallback to last tick: yes_ask=50 → 0.50
        assert result["fill_price"] == pytest.approx(0.50)


class TestTakerFillModelFee:
    def test_fee_is_3pct_of_fill_price(self):
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01", yes_ask=60)
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert result["fee"] == pytest.approx(0.03 * result["fill_price"])

    def test_fee_rate_configurable(self):
        model = TakerFillModel(latency_ms=0.0, fee_rate=0.01)
        tick = _tick("2024-01-01", yes_ask=80)
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert result["fee"] == pytest.approx(0.01 * result["fill_price"])


class TestTakerFillModelSlippage:
    def test_slippage_equals_half_spread(self):
        """Slippage = |fill_price - mid|. Mid = (bid+ask)/2."""
        model = TakerFillModel(latency_ms=0.0)
        # yes_bid=40, yes_ask=60 → mid=50c=0.50, fill=60c=0.60 → slippage=0.10
        tick = _tick("2024-01-01", yes_bid=40, yes_ask=60)
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert result["slippage"] == pytest.approx(0.10)

    def test_slippage_no_side(self):
        """No-side slippage: |no_ask/100 - (no_bid+no_ask)/200|."""
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01", no_bid=30, no_ask=70)
        result = model.fill("no", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        # mid = (30+70)/200 = 0.50, fill = 70/100 = 0.70 → slippage = 0.20
        assert result["slippage"] == pytest.approx(0.20)


class TestTakerFillModelReturnSchema:
    def test_return_dict_has_expected_keys(self):
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01")
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert set(result.keys()) == {"fill_price", "fee", "slippage", "timestamp_filled"}

    def test_timestamp_filled_is_pandas_timestamp(self):
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01")
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert isinstance(result["timestamp_filled"], pd.Timestamp)


# ── MakerFillModel ────────────────────────────────────────────────────────────


class TestMakerFillProbability:
    def test_fill_probability_less_than_half(self):
        """With positive price_improvement, logistic gives prob < 0.5."""
        model = MakerFillModel(price_improvement_cents=1.0)
        tick = _tick("2024-01-01", yes_bid=45, yes_ask=55)  # spread=10
        prob = model.fill_probability("yes", tick)
        assert 0.0 < prob < 0.5

    def test_zero_spread_returns_half(self):
        """When spread=0, fill_probability returns 0.5."""
        model = MakerFillModel(price_improvement_cents=1.0)
        tick = _tick("2024-01-01", yes_bid=50, yes_ask=50)  # zero spread
        prob = model.fill_probability("yes", tick)
        assert prob == pytest.approx(0.5)

    def test_larger_improvement_lower_prob(self):
        """More price improvement relative to spread → lower fill probability."""
        model_small = MakerFillModel(price_improvement_cents=0.5)
        model_large = MakerFillModel(price_improvement_cents=5.0)
        tick = _tick("2024-01-01", yes_bid=45, yes_ask=55)
        assert model_large.fill_probability("yes", tick) < model_small.fill_probability("yes", tick)


class TestMakerFillEmpty:
    def test_empty_ticks_returns_none(self):
        model = MakerFillModel()
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [])
        assert result is None


class TestMakerFillProbabilityGate:
    def test_fill_returns_none_when_rng_above_prob(self):
        """Seed RNG so that rng.random() > fill_probability → returns None."""
        model = MakerFillModel(price_improvement_cents=1.0)
        tick = _tick("2024-01-01", yes_bid=45, yes_ask=55)
        # fill_probability ≈ 0.377 for x=1/10; force rng to return 0.99 → no fill
        rng = np.random.default_rng(0)
        # Manually find a seed that causes no fill
        # fill_prob ≈ 0.377; we need rng.random() > 0.377
        # Use a mocked rng that always returns 0.99
        class _HighRNG:
            def random(self):
                return 0.99
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick], rng=_HighRNG())
        assert result is None

    def test_fill_succeeds_when_rng_below_prob(self):
        """Seed RNG so that rng.random() < fill_probability → returns fill dict."""
        model = MakerFillModel(price_improvement_cents=1.0)
        tick = _tick("2024-01-01", yes_bid=45, yes_ask=55)
        class _LowRNG:
            def random(self):
                return 0.0
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick], rng=_LowRNG())
        assert result is not None


class TestMakerFillAdverseSelection:
    def test_adverse_selection_in_fill_price(self):
        """Fill price = limit_price + adverse_selection_fraction * spread."""
        model = MakerFillModel(
            price_improvement_cents=1.0,
            adverse_selection_fraction=0.4,
        )
        tick = _tick("2024-01-01", yes_bid=45, yes_ask=55)
        # limit = yes_bid + price_improvement = 45 + 1 = 46 cents → 0.46
        # spread = |55 - 45| / 100 = 0.10
        # adverse = 0.4 * 0.10 = 0.04
        # fill_price = 0.46 + 0.04 = 0.50
        class _LowRNG:
            def random(self):
                return 0.0
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick], rng=_LowRNG())
        assert result is not None
        assert result["fill_price"] == pytest.approx(0.50)
        assert result["slippage"] == pytest.approx(0.04)

    def test_maker_fee_is_zero(self):
        """Kalshi maker fee = 0."""
        model = MakerFillModel(fee_rate=0.0)
        tick = _tick("2024-01-01")
        class _LowRNG:
            def random(self):
                return 0.0
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick], rng=_LowRNG())
        assert result is not None
        assert result["fee"] == 0.0


class TestPriceClipping:
    def test_taker_clips_low_price_to_001(self):
        """yes_ask=0 → fill_price clipped to 0.01."""
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01", yes_bid=0, yes_ask=0)
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert result["fill_price"] == pytest.approx(0.01)

    def test_taker_clips_high_price_to_099(self):
        """yes_ask=100 → fill_price clipped to 0.99."""
        model = TakerFillModel(latency_ms=0.0)
        tick = _tick("2024-01-01", yes_bid=99, yes_ask=100)
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick])
        assert result is not None
        assert result["fill_price"] == pytest.approx(0.99)

    def test_maker_clips_fill_price_to_valid_range(self):
        """Maker fill price clipped to [0.01, 0.99]."""
        model = MakerFillModel(
            price_improvement_cents=5.0,
            adverse_selection_fraction=0.0,
        )
        tick = _tick("2024-01-01", yes_bid=0, yes_ask=1)  # very low tick
        class _LowRNG:
            def random(self):
                return 0.0
        result = model.fill("yes", pd.Timestamp("2024-01-01", tz="UTC"), [tick], rng=_LowRNG())
        assert result is not None
        assert 0.01 <= result["fill_price"] <= 0.99
