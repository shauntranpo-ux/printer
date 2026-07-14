"""Regression check: the v3 gates block the trades that actually lost.

Deliberately loose directional bounds (not a curve fit): the vendored 52-row export
is the live paper record 2026-06-30..07-04 whose new-era slice went 5W-32L, -$418.63.
"""
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "trades_export_2026-06-30_07-04.csv")

pytestmark = pytest.mark.skipif(not os.path.exists(FIXTURE),
                                reason="trade export fixture not present")


@pytest.fixture(scope="module")
def replayed():
    from scripts.replay_gates import load_rows, replay
    rows = load_rows(FIXTURE)
    results, summary = replay(rows)
    return rows, results, summary


def test_blocks_most_new_era_losers(replayed):
    _, results, _ = replayed
    losers = [(r, b) for r, b in results if r.get("outcome") == "loss"]
    blocked = sum(1 for _, b in losers if b)
    assert losers, "fixture must contain new-era losers"
    assert blocked / len(losers) >= 0.70, f"only {blocked}/{len(losers)} losers blocked"


def test_surviving_pnl_beats_original(replayed):
    rows, _, summary = replayed
    original = sum(r["_pnl"] for r in rows if r["_new_era"])
    assert summary["surviving_pnl"] > original


def test_at_least_one_winner_survives(replayed):
    _, _, summary = replayed
    assert summary["winners_surviving"] >= 1


def test_every_gate_fires_at_least_once(replayed):
    _, _, summary = replayed
    for gate in ("tgtbt", "tail_ban", "entry_band", "time_window", "vol_anchor"):
        assert summary["gate_counts"].get(gate, 0) > 0, f"{gate} never fired on the record"
