"""/api/edge v3 blocks: shadow (s_fav promotion scoreboard) and sigma engine state."""
import sys, os, sqlite3, asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state


@pytest.fixture()
def seeded_server(monkeypatch, tmp_path):
    db_path = str(tmp_path / "edge.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db_path)
    monkeypatch.setenv("BOT_DB_FILE", db_path)
    import bot_infra
    bot_infra.init_db()
    con = sqlite3.connect(db_path)
    for i in range(5):
        con.execute(
            "INSERT INTO decision_log (ts,ticker,asset,strategy,mode,side,model_p_yes,"
            "market_mid_p_yes,market_edge,entry_price_cents,secs_left,would_trade,outcome,z) "
            "VALUES ('2026-07-04T01:00:00Z',?,'SOL','s_fav','paper','yes',0.84,0.75,0.05,"
            "76,300,0,?,1.0)",
            (f"KX-F{i}", "yes" if i < 4 else "no"))
    con.commit()
    con.close()
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    return server.app.test_client()


def test_shadow_block_scores_s_fav_rows(seeded_server):
    d = seeded_server.get("/api/edge").get_json()
    sf = d["shadow"]["s_fav"]
    assert sf["n"] == 5
    assert sf["win_rate"] == pytest.approx(0.8)
    assert sf["premium"] == pytest.approx(0.05)          # realized 0.80 vs market 0.75
    assert "collecting" in d["shadow"]["verdict"]


def test_sigma_block_carries_static_bases(seeded_server):
    d = seeded_server.get("/api/edge").get_json()
    assert set(d["sigma"]) >= {"BTC", "ETH", "SOL", "XRP", "DOGE"}
    assert d["sigma"]["SOL"]["static"] == pytest.approx(0.0046)


def test_empty_db_returns_safe_shape(seeded_server, monkeypatch, tmp_path):
    empty = str(tmp_path / "empty.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", empty)
    monkeypatch.setenv("BOT_DB_FILE", empty)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    d = server.app.test_client().get("/api/edge").get_json()
    assert d["shadow"]["s_fav"] is None
    assert d["shadow"]["verdict"] == "insufficient data"
    assert isinstance(d["sigma"], dict)
