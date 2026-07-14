"""shadow_fav_candidate: fires only on late-window, spot-confirmed favorites."""
import sys, os, time, collections
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import shadow_fav_candidate

CFG = {"shadow_fav_enabled": True, "settlement_avg_seconds": 60}


@pytest.fixture(autouse=True)
def _clean():
    saved = asset_manager._prices.get("SOL")
    bot_state._implied_sigma.clear()
    bot_state._sigma_scale.clear()
    yield
    if saved is not None:
        asset_manager._prices["SOL"] = saved
    bot_state._implied_sigma.clear()
    bot_state._sigma_scale.clear()


def _seed(last, strike, above=True):
    now = time.time()
    dq = collections.deque(maxlen=2000)
    start = strike * (1.001 if above else 0.999)
    for i in range(40):
        dq.append((now - (39 - i) * 2.0, start + (last - start) * (i / 39.0)))
    asset_manager._prices["SOL"] = dq


def _run(spot=150.4, strike=150.0, yes_ask=76.0, no_ask=26.0, secs=300.0, cfg=None):
    _seed(spot, strike, above=spot >= strike)
    with patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        return shadow_fav_candidate("SOL", spot, strike, yes_ask, no_ask, secs, cfg or CFG)


def test_fires_on_late_window_favorite():
    out = _run()
    assert out is not None
    assert out["strategy"] == "s_fav"
    assert out["side"] == "yes"
    assert out["would_trade"] is False
    assert out["z"] >= 0.8
    assert out["sigma_eff"] > 0
    assert out["entry_price_cents"] == 76.0


def test_none_outside_time_window():
    assert _run(secs=480.0) is None       # 8 min: too early
    assert _run(secs=150.0) is None       # 2.5 min: too late


def test_none_when_z_small():
    assert _run(spot=150.05) is None


def test_none_when_favorite_too_expensive():
    assert _run(yes_ask=93.0, no_ask=9.0) is None    # mid ~0.92 > 0.88


def test_none_when_side_not_a_favorite():
    assert _run(yes_ask=55.0, no_ask=47.0) is None   # mid ~0.54 < 0.70


def test_none_when_disabled():
    assert _run(cfg={**CFG, "shadow_fav_enabled": False}) is None


def test_none_on_spot_flicker():
    now = time.time()
    dq = collections.deque(maxlen=2000)
    for i in range(40):
        dq.append((now - (39 - i) * 2.0, 150.0 + (0.4 if i % 2 == 0 else -0.4)))
    asset_manager._prices["SOL"] = dq
    with patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        out = shadow_fav_candidate("SOL", 150.4, 150.0, 76.0, 26.0, 300.0, CFG)
    assert out is None
