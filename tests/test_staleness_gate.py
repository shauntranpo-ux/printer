"""_staleness_check: stale_ok / fresh_book / unknown statuses and sign consistency."""
import sys, os, time, collections

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs

SIGMA = 0.0046
CFG = {"staleness_gate_enabled": True, "staleness_window_secs": 60.0,
       "staleness_min_spot_sigma": 0.35, "staleness_max_mid_move_cents": 3.0}


@pytest.fixture(autouse=True)
def _clean():
    saved = asset_manager._prices.get("SOL")
    bot_state._contract_mid_history.clear()
    yield
    if saved is not None:
        asset_manager._prices["SOL"] = saved
    bot_state._contract_mid_history.clear()


def _seed_spot(move_60s: float, span: float = 90.0):
    """Spot deque spanning `span` seconds whose last-60s log move is ~move_60s."""
    now = time.time()
    dq = collections.deque(maxlen=2000)
    base = 150.0
    n = 45
    for i in range(n):
        t = now - span + i * (span / (n - 1))
        age = now - t
        frac_of_move = max(0.0, 1.0 - age / 60.0)   # move happens inside the last 60s
        dq.append((t, base * (2.718281828 ** (move_60s * frac_of_move))))
    asset_manager._prices["SOL"] = dq


def _seed_mids(ticker: str, mid_then: float, mid_now: float):
    now = time.time()
    hist = collections.deque(maxlen=120)
    hist.append((now - 60.0, mid_then))
    hist.append((now - 30.0, (mid_then + mid_now) / 2.0))
    hist.append((now, mid_now))
    bot_state._contract_mid_history[ticker] = hist


def test_stale_ok_spot_moved_book_did_not():
    _seed_spot(0.003)                      # +0.3% in the last minute, well over the floor
    _seed_mids("KX-T1", 60.0, 61.0)        # mid moved 1c < 3c
    status, info = bs._staleness_check("SOL", "KX-T1", "yes", SIGMA, CFG)
    assert status == "stale_ok"
    assert info["spot_move"] > 0
    assert info["mid_hist_n"] == 3


def test_fresh_book_when_mid_already_repriced():
    _seed_spot(0.003)
    _seed_mids("KX-T2", 60.0, 67.0)        # mid moved 7c: the book followed the spot
    status, info = bs._staleness_check("SOL", "KX-T2", "yes", SIGMA, CFG)
    assert status == "fresh_book"
    assert info["mid_move"] >= 3.0


def test_fresh_book_when_spot_did_not_move():
    _seed_spot(0.00001)                    # no qualifying move: nothing is stale
    _seed_mids("KX-T3", 60.0, 60.5)
    status, _ = bs._staleness_check("SOL", "KX-T3", "yes", SIGMA, CFG)
    assert status == "fresh_book"


def test_sign_consistency_move_against_side_is_fresh():
    _seed_spot(-0.003)                     # spot moved DOWN; buying YES is not a lag trade
    _seed_mids("KX-T4", 60.0, 60.5)
    status, _ = bs._staleness_check("SOL", "KX-T4", "yes", SIGMA, CFG)
    assert status == "fresh_book"
    # ... but the same move supports the NO side.
    status_no, _ = bs._staleness_check("SOL", "KX-T4", "no", SIGMA, CFG)
    assert status_no == "stale_ok"


def test_unknown_without_mid_history():
    _seed_spot(0.003)
    status, info = bs._staleness_check("SOL", "KX-NOHIST", "yes", SIGMA, CFG)
    assert status == "unknown"
    assert info["mid_hist_n"] == 0


def test_unknown_when_spot_anchor_out_of_window():
    now = time.time()
    dq = collections.deque(maxlen=2000)
    for i in range(10):                    # only ~10s of history: no 60s anchor
        dq.append((now - 10 + i, 150.0))
    asset_manager._prices["SOL"] = dq
    _seed_mids("KX-T5", 60.0, 60.5)
    status, _ = bs._staleness_check("SOL", "KX-T5", "yes", SIGMA, CFG)
    assert status == "unknown"


def test_unknown_when_gate_disabled():
    _seed_spot(0.003)
    _seed_mids("KX-T6", 60.0, 67.0)
    status, _ = bs._staleness_check("SOL", "KX-T6", "yes", SIGMA,
                                    {**CFG, "staleness_gate_enabled": False})
    assert status == "unknown"
