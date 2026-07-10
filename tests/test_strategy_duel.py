"""
The duel: S1 momentum and S2 favorite-bias are opposite bets that diverge on the same
tape, both trade every market, and their head-to-head is surfaced (labels + edge report).
"""
import sys, os, time, sqlite3, subprocess, collections
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asset_manager
import bot_state
import bot_stats
import bot_strategy as bs


_CFG = {"mode": "paper", "quiet_hours_enabled": False, "calibration_enabled": False,
        "auto_gate_enabled": False, "staleness_gate_enabled": False}


def _seed_up(asset, last, ret, n=60, span=90.0):
    now = time.time()
    start = last / (2.718281828 ** ret)
    dq = collections.deque(maxlen=2000)
    for i in range(n):
        t = now - (n - 1 - i) * (span / (n - 1))
        dq.append((t, start + (last - start) * (i / (n - 1))))
    asset_manager._prices[asset] = dq
    return dq


def test_brains_diverge_on_same_mid_window_move():
    """A fresh mid-window up-move: S1 (momentum) trades YES; S2 (favorite) skips - too
    early for the favorite harvest. Same tape, different decisions."""
    saved = asset_manager._prices.get("SOL")
    try:
        _seed_up("SOL", last=150.5, ret=0.004)
        with patch("bot_strategy.read_config", return_value=_CFG), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            # 7 min left: inside S1's window (3-10), outside S2's late window (2.5-6).
            s1 = bs.strategy_brain_s1(150.5, 150.45, 58.0, 45.0, 480, 420, "SOL-D", asset="SOL")
            s2 = bs.strategy_brain_s2(150.5, 150.45, 58.0, 45.0, 480, 420, "SOL-D", asset="SOL")
        assert s1["action"] == "trade" and s1["side"] == "yes", s1["reasoning"]
        assert s2["action"] == "skip", s2["reasoning"]
        assert s1["strategy_variant"] == "strategy1"
        assert s2["strategy_variant"] == "strategy2"
    finally:
        if saved is not None:
            asset_manager._prices["SOL"] = saved


def test_strategy_labels_are_momentum_and_favorite():
    assert bot_stats._STRATEGY_LABELS["strategy1"].endswith("Momentum")
    assert "Favorite" in bot_stats._STRATEGY_LABELS["strategy2"]


def test_server_edge_exposes_head_to_head():
    """The /api/edge aggregation must compute a per-strategy total and a head-to-head."""
    import inspect, server
    src = inspect.getsource(server)
    assert "head_to_head" in src, "server /api/edge must build a head_to_head block"
    assert "total_pnl" in src, "per-strategy total_pnl must feed the head-to-head"


def _seed_decision_db(path, strat, side, outcome, entry_cents, n):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS decision_log (
        id INTEGER PRIMARY KEY, ts TEXT, ticker TEXT, asset TEXT, strategy TEXT,
        mode TEXT, side TEXT, model_p_yes REAL, market_mid_p_yes REAL, market_edge REAL,
        entry_price_cents REAL, secs_left REAL, would_trade INTEGER, outcome TEXT)""")
    rows = [("2026-07-04T14:00:00+00:00", f"KX-{strat}-{i}", "SOL", strat, "paper",
             side, 0.8, 0.75, 0.05, entry_cents, 240, 1, outcome) for i in range(n)]
    con.executemany(
        "INSERT INTO decision_log (ts,ticker,asset,strategy,mode,side,model_p_yes,"
        "market_mid_p_yes,market_edge,entry_price_cents,secs_left,would_trade,outcome) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_edge_report_prints_head_to_head_winner(tmp_path):
    """edge_report must print a HEAD-TO-HEAD block and name a winner when both strategies
    have settled picks. S1 here wins every pick (entry 50c, outcome matches side); S2
    loses every pick, so S1 must be the named winner."""
    db = str(tmp_path / "edge.db")
    _seed_decision_db(db, "strategy1", "yes", "yes", 50.0, 20)   # S1 wins all
    _seed_decision_db(db, "strategy2", "yes", "no", 78.0, 20)    # S2 loses all
    out = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                      "scripts", "edge_report.py"), db],
        capture_output=True, text=True, timeout=60).stdout
    assert "HEAD-TO-HEAD" in out, out
    assert "WINNER: S1 Momentum" in out, out
