"""/api/edge surfaces the harness (decision_log + maker_log) for the dashboard GATE-1 panel."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import bot_infra


def _client(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    monkeypatch.setenv("BOT_DB_FILE", db)
    bot_infra.init_db()
    import server
    return server.app.test_client(), db


def test_edge_empty_db_is_insufficient_not_500(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/api/edge")
    assert r.status_code == 200
    d = r.get_json()
    assert d["decisions"]["verdict"] == "insufficient data"
    assert d["decisions"]["overall"] is None
    assert d["counts"] == {"logged": 0, "settled": 0, "pending": 0}
    assert d["maker"]["verdict"] == "insufficient data"


def test_edge_reports_positive_edge_on_seeded_picks(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    # 300 picks at a true 60% win rate, model says 0.60 vs market 0.50 @ 50c
    for i in range(300):
        out = "yes" if (i % 5) < 3 else "no"  # 60% yes
        conn.execute(
            "INSERT INTO decision_log(ts,ticker,asset,strategy,mode,side,model_p_yes,"
            "market_mid_p_yes,entry_price_cents,secs_left,would_trade,outcome) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"2026-06-2{i%9}T00:00:00+00:00", f"P{i}", "ETH", "strategy1", "paper",
             "yes", 0.60, 0.50, 50.0, 300, 1, out))
    conn.commit()
    conn.close()
    d = client.get("/api/edge").get_json()
    ov = d["decisions"]["overall"]
    assert ov["n"] == 300
    assert ov["mean_model_p"] == 0.60 and ov["mean_market_p"] == 0.50
    assert ov["net_pnl_per_contract"] > 0 and ov["wilson_lb_pnl"] > 0
    assert d["decisions"]["verdict"].startswith("edge: net positive")
    assert d["decisions"]["by_strategy"]["strategy1"]["n"] == 300
    assert d["counts"]["settled"] == 300


def test_edge_maker_section_and_no_nan_in_json(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    for i in range(3):  # tiny maker sample -> delta_se may be NaN; must serialize as null
        conn.execute(
            "INSERT INTO maker_log(strategy,asset,mode,side,entry_ask_cents,maker_price_cents,"
            "filled,outcome,taker_pnl,maker_pnl,contracts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("strategy2", "ETH", "paper", "yes", 45.0, 44.0, i % 2, "win" if i % 2 else "loss",
             -0.1, 0.5 if i % 2 else None, 50))
    conn.commit()
    conn.close()
    # raw body must be valid JSON (no bare NaN tokens)
    raw = client.get("/api/edge").get_data(as_text=True)
    assert "NaN" not in raw
    d = client.get("/api/edge").get_json()
    assert d["maker"]["overall"]["n"] == 3
    assert "insufficient data" in d["maker"]["verdict"]
