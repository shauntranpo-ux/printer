"""_pick_best_strike selects the SMALLEST model-vs-market gap among firing candidates."""
import sys, os, asyncio
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_loops


def _mk_market(ticker, strike, secs=480.0):
    return {"ticker": ticker, "strike": strike, "secs": secs}


def _mk_brain(action, gap=None, spread=1.0):
    b = {"action": action, "side": "yes", "signals": {}}
    if gap is not None:
        b["signals"] = {"gap": gap, "spread_cents": spread, "ev": 0.05}
    return b


def _run_pick(candidates, brains, default):
    cfg = {"ladder_max_strikes": 3}
    brains_iter = iter(brains)
    with patch.object(bot_loops, "fetch_market_for_asset",
                      AsyncMock(return_value=candidates)), \
         patch.object(bot_loops, "fetch_orderbook",
                      AsyncMock(return_value={"best_yes_ask": 73.0, "best_no_ask": 29.0, "obi": 0.0})), \
         patch.object(bot_loops, "parse_strike", side_effect=lambda m: m["strike"]), \
         patch.object(bot_loops, "seconds_remaining", side_effect=lambda m: m["secs"]), \
         patch.object(bot_loops, "seconds_elapsed", side_effect=lambda m: 900.0 - m["secs"]), \
         patch.object(bot_loops, "strategy_brain_s2", side_effect=lambda *a, **k: next(brains_iter)), \
         patch.object(bot_loops, "track_contract_mid"), \
         patch.object(bot_loops, "update_implied_sigma"):
        return asyncio.run(bot_loops._pick_best_strike(None, cfg, "SOL", 150.4, default))


def test_picks_smallest_gap_not_biggest_ev():
    default = _mk_market("KX-DEF", 150.0)
    cands = [_mk_market("KX-DEF", 150.0), _mk_market("KX-FAR", 151.0)]
    # The far strike shows the BIGGER gap (the old max-EV pick); the near one must win.
    brains = [_mk_brain("trade", gap=0.06), _mk_brain("trade", gap=0.13)]
    got = _run_pick(cands, brains, default)
    assert got["ticker"] == "KX-DEF"


def test_switches_to_smaller_gap_candidate():
    default = _mk_market("KX-DEF", 150.0)
    cands = [_mk_market("KX-DEF", 150.0), _mk_market("KX-NEAR", 150.2)]
    brains = [_mk_brain("trade", gap=0.12), _mk_brain("trade", gap=0.05)]
    got = _run_pick(cands, brains, default)
    assert got["ticker"] == "KX-NEAR"


def test_tie_breaks_to_tighter_spread():
    default = _mk_market("KX-DEF", 150.0)
    cands = [_mk_market("KX-A", 150.0), _mk_market("KX-B", 150.2)]
    brains = [_mk_brain("trade", gap=0.06, spread=3.0), _mk_brain("trade", gap=0.06, spread=1.0)]
    got = _run_pick(cands, brains, default)
    assert got["ticker"] == "KX-B"


def test_falls_back_when_no_candidate_fires():
    default = _mk_market("KX-DEF", 150.0)
    cands = [_mk_market("KX-A", 150.0), _mk_market("KX-B", 150.2)]
    brains = [_mk_brain("skip"), _mk_brain("skip")]
    got = _run_pick(cands, brains, default)
    assert got["ticker"] == "KX-DEF"
