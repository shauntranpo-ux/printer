"""Step 3A: maker-vs-taker counterfactual measurement (fill logic, fee, report math)."""
import collections
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import bot_infra
import bot_loops


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_state, "_DB_FILE", str(tmp_path / "t.db"))
    bot_infra.init_db()


def _track(ticker, rows):
    bot_state._maker_track[ticker] = collections.deque(rows, maxlen=120)


async def test_maker_fills_when_ask_reaches_price(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    # YES entry at ask=45c -> maker posts 44c. Held-book yes_ask later dips to 43c -> FILLS.
    _track("KX-A", [(100.0, 45.0, 56.0), (110.0, 43.0, 58.0), (120.0, 47.0, 54.0)])
    pos = {"ticker": "KX-A", "side": "yes", "entry_price_cents": 45.0,
           "entry_ts": 99.0, "contracts": 50}
    await bot_loops._record_maker_counterfactual(pos, "ETH", "win", {"mode": "paper"})
    conn = sqlite3.connect(bot_state._DB_FILE)
    row = conn.execute("SELECT filled, maker_price_cents, outcome, maker_pnl, taker_pnl "
                       "FROM maker_log").fetchone()
    conn.close()
    assert row[0] == 1                      # filled
    assert abs(row[1] - 44.0) < 1e-9        # maker price = 1c inside ask
    # win at maker 44c: payoff 1.0 - 0.44 - fee(0.44) ; > taker pnl (paid 45c)
    assert row[3] > row[4]
    # _maker_track cleaned up
    assert "KX-A" not in bot_state._maker_track


async def test_maker_does_not_fill_when_ask_never_reaches(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    # ask stays >= 45 the whole time -> a 44c maker never fills
    _track("KX-B", [(100.0, 45.0, 56.0), (110.0, 46.0, 55.0), (120.0, 47.0, 54.0)])
    pos = {"ticker": "KX-B", "side": "yes", "entry_price_cents": 45.0,
           "entry_ts": 99.0, "contracts": 50}
    await bot_loops._record_maker_counterfactual(pos, "ETH", "loss", {"mode": "paper"})
    conn = sqlite3.connect(bot_state._DB_FILE)
    row = conn.execute("SELECT filled, maker_pnl, taker_pnl FROM maker_log").fetchone()
    conn.close()
    assert row[0] == 0                      # not filled
    assert row[1] is None                   # no maker pnl (didn't trade)
    assert row[2] < 0                       # taker took the loss


async def test_no_side_uses_no_ask_path(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    # NO entry at no_ask=40 -> maker 39. no_ask dips to 38 -> fills.
    _track("KX-C", [(100.0, 62.0, 40.0), (110.0, 62.0, 38.0)])
    pos = {"ticker": "KX-C", "side": "no", "entry_price_cents": 40.0,
           "entry_ts": 99.0, "contracts": 25}
    await bot_loops._record_maker_counterfactual(pos, "SOL", "win", {"mode": "paper"})
    conn = sqlite3.connect(bot_state._DB_FILE)
    filled = conn.execute("SELECT filled FROM maker_log").fetchone()[0]
    conn.close()
    assert filled == 1


async def test_pre_entry_ticks_do_not_count(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    # a low ask BEFORE entry_ts must not count as a fill
    _track("KX-D", [(50.0, 40.0, 60.0), (110.0, 46.0, 55.0)])  # 40c tick is pre-entry
    pos = {"ticker": "KX-D", "side": "yes", "entry_price_cents": 45.0,
           "entry_ts": 100.0, "contracts": 10}
    await bot_loops._record_maker_counterfactual(pos, "ETH", "loss", {"mode": "paper"})
    conn = sqlite3.connect(bot_state._DB_FILE)
    filled = conn.execute("SELECT filled FROM maker_log").fetchone()[0]
    conn.close()
    assert filled == 0


async def test_never_raises_on_missing_track(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    pos = {"ticker": "KX-NONE", "side": "yes", "entry_price_cents": 45.0,
           "entry_ts": 99.0, "contracts": 10}
    await bot_loops._record_maker_counterfactual(pos, "ETH", "loss", {"mode": "paper"})  # no track
    conn = sqlite3.connect(bot_state._DB_FILE)
    row = conn.execute("SELECT filled FROM maker_log").fetchone()
    conn.close()
    assert row[0] == 0


def test_maker_fee_is_quarter_of_taker():
    # maker fee 0.0175*p*(1-p) is 25% of the 0.07 taker fee at the same price
    assert abs(bot_loops._maker_fee_frac(50.0) - 0.0175 * 0.25) < 1e-9
    assert abs(bot_loops._maker_fee_frac(50.0) / (0.07 * 0.5 * 0.5) - 0.25) < 1e-9


def test_maker_report_math():
    from scripts.maker_report import _stats
    rows = [
        {"filled": 1, "taker_pnl": -0.20, "maker_pnl": 0.55},   # maker fills a winner cheaper
        {"filled": 0, "taker_pnl": -0.45, "maker_pnl": None},   # maker skips a loser -> $0
        {"filled": 1, "taker_pnl": 0.55, "maker_pnl": 0.56},
    ]
    s = _stats(rows)
    assert s["n"] == 3
    assert abs(s["fill_rate"] - 2 / 3) < 1e-9
    # maker strategy: 0.55, 0.0, 0.56 -> mean 0.37 ; taker: -0.20,-0.45,0.55 -> mean -0.0333
    assert abs(s["ms_mean"] - (0.55 + 0.0 + 0.56) / 3) < 1e-9
    assert s["delta"] > 0
