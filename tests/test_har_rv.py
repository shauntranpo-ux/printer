import numpy as np
import pytest
from strategy_a.features.har_rv import HARRSJForecaster

_CFG = {
    "returns": {"granularity_seconds": 10},
    "har_rs_j": {
        "timescales_minutes": [15, 60, 240],
        "coefficients": {
            "const": None, "rv_15m_pos": None, "rv_15m_neg": None,
            "rv_1h_pos": None, "rv_1h_neg": None,
            "rv_4h_pos": None, "rv_4h_neg": None, "jump": None,
        },
    },
}

_EXPECTED_KEYS = {
    "15m_rv", "15m_rv_pos", "15m_rv_neg", "15m_bv", "15m_jump", "15m_signed_jump",
    "1h_rv",  "1h_rv_pos",  "1h_rv_neg",  "1h_bv",  "1h_jump",  "1h_signed_jump",
    "4h_rv",  "4h_rv_pos",  "4h_rv_neg",  "4h_bv",  "4h_jump",  "4h_signed_jump",
    "sigma_forecast",
}

def _returns(n=300):
    rng = np.random.default_rng(42)
    return rng.normal(0, 0.001, n)


def test_smoke():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    assert isinstance(f.compute(), dict)


def test_shape():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    assert _EXPECTED_KEYS.issubset(f.compute().keys())


def test_type_and_range():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    result = f.compute()
    for k, v in result.items():
        assert isinstance(v, float), f"{k} is not float"
        assert np.isfinite(v), f"{k} not finite: {v}"
    for scale in ("15m", "1h", "4h"):
        assert result[f"{scale}_rv"]     >= 0.0
        assert result[f"{scale}_rv_pos"] >= 0.0
        assert result[f"{scale}_rv_neg"] >= 0.0
        assert result[f"{scale}_jump"]   >= 0.0
    assert result["sigma_forecast"] >= 0.0


def test_online_update_matches_batch():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.001, 100).tolist()
    f_batch = HARRSJForecaster(_CFG)
    f_batch.fit(np.array(rets))
    r_batch = f_batch.compute()

    f_online = HARRSJForecaster(_CFG)
    for r in rets:
        f_online.update({"log_return": r})
    r_online = f_online.compute()

    assert abs(r_batch["15m_rv"] - r_online["15m_rv"]) < 1e-12


def test_rv_plus_rv_neg_sum_to_rv():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    res = f.compute()
    for scale in ("15m", "1h", "4h"):
        total = res[f"{scale}_rv_pos"] + res[f"{scale}_rv_neg"]
        assert abs(total - res[f"{scale}_rv"]) < 1e-12, (
            f"{scale}: RV+={res[f'{scale}_rv_pos']} + RV-={res[f'{scale}_rv_neg']} "
            f"!= RV={res[f'{scale}_rv']}"
        )
