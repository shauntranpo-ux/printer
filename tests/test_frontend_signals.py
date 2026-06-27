"""server._frontend_signals maps bot eval -> dashboard Decision Signals schema."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import _frontend_signals


def _base(direction, **kw):
    a = {
        "direction": direction,
        "ev": 5.5,
        "win_prob": 39.0,
        "status": "TRADING",
        "skip_reason": "",
        "s1_gates": {"passed": 2, "total": 6},
        "s2_gates": {"passed": 3, "total": 6},
        "s1_dir": "UP",
        "s2_dir": "UP",
        "signals": {"mkt_p": 0.31, "model_raw_p_yes": 0.58},
    }
    a.update(kw)
    return a


def test_yes_side_projects_directly():
    s = _frontend_signals(_base("UP"))
    assert abs(s["market_prob"] - 0.31) < 1e-9   # mkt_p is already YES-side
    assert abs(s["p_ev"] - 0.39) < 1e-9          # win_prob 39% -> 0.39 YES
    assert s["raw_p_yes"] == 0.58
    assert s["yes_ev"] == 5.5 and s["no_ev"] == 0.0
    assert s["final_decision"] == "trade"
    assert s["vote_count"] == 5                   # 2 + 3, capped at 5
    assert s["ev_pass"] is True
    assert s["supertrend"] == 1 and s["velocity"] == "rising"


def test_no_side_projects_complement():
    # direction DOWN: side-relative probs are NO-side, project to YES via complement
    s = _frontend_signals(_base("DOWN", s1_dir="DOWN", s2_dir="DOWN"))
    assert abs(s["market_prob"] - 0.69) < 1e-9   # 1 - 0.31
    assert abs(s["p_ev"] - 0.61) < 1e-9          # 1 - 0.39
    assert s["no_ev"] == 5.5 and s["yes_ev"] == 0.0
    assert s["supertrend"] == -1 and s["velocity"] == "falling"


def test_offline_asset_defaults_are_numeric():
    s = _frontend_signals({"phase": "OFFLINE"})
    # never None — frontend calls .toFixed on EVs and *100 on probs
    for k in ("raw_p_yes", "p_ev", "market_prob"):
        assert s[k] == 0.5
    assert s["yes_ev"] == 0.0 and s["no_ev"] == 0.0
    assert s["final_decision"] == "skip"
    assert s["ev_pass"] is False
