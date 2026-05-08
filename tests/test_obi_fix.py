"""tests/test_obi_fix.py — OBI fix: Kalshi contract depth replaces Coinbase WebSocket OBI."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_ticker_obi():
    """Wipe _ticker_obi before each test so tests don't bleed state."""
    import bot_state
    if hasattr(bot_state, "_ticker_obi"):
        bot_state._ticker_obi.clear()
    yield
    if hasattr(bot_state, "_ticker_obi"):
        bot_state._ticker_obi.clear()


# ── _kalshi_obi unit tests ────────────────────────────────────────────────────

def test_kalshi_obi_positive_when_no_heavy():
    """no_depth > yes_depth → positive OBI (bullish for YES)."""
    from bot_market import _kalshi_obi
    yes_arr = [[60, 10], [65, 5]]    # yes_depth = 15
    no_arr  = [[40, 20], [45, 10]]   # no_depth  = 30
    result = _kalshi_obi(yes_arr, no_arr)
    assert result is not None
    assert result > 0
    assert abs(result - (30 - 15) / (30 + 15)) < 1e-9


def test_kalshi_obi_negative_when_yes_heavy():
    """yes_depth > no_depth → negative OBI (bearish for YES)."""
    from bot_market import _kalshi_obi
    yes_arr = [[60, 30], [65, 10]]   # yes_depth = 40
    no_arr  = [[40, 5],  [45, 5]]    # no_depth  = 10
    result = _kalshi_obi(yes_arr, no_arr)
    assert result is not None
    assert result < 0
    assert abs(result - (10 - 40) / (10 + 40)) < 1e-9


def test_kalshi_obi_empty_arrays():
    """Both empty → None (AMM market / no depth data)."""
    from bot_market import _kalshi_obi
    assert _kalshi_obi([], []) is None


def test_kalshi_obi_top_n_capping():
    """Only top 5 price levels (lowest ask first) contribute to depth."""
    from bot_market import _kalshi_obi
    # YES: 10 levels at prices 60-69, qty 100 each → yes_depth=1000 without capping, 500 with top_n=5
    # NO: 5 levels at prices 40-44 (qty 100), then 5 levels at 45-49 (qty 900 each)
    # Without capping: no_depth = 5*100 + 5*900 = 5000 → heavy NO side
    # With top_n=5: no_depth = 5*100 = 500 → balanced → OBI=0.0
    yes_arr = [[60 + i, 100] for i in range(10)]
    no_arr  = [[40 + i, 100] for i in range(5)] + [[45 + i, 900] for i in range(5)]
    result = _kalshi_obi(yes_arr, no_arr, top_n=5)
    # top 5 yes (lowest): 60,61,62,63,64 → yes_depth = 500
    # top 5 no  (lowest): 40,41,42,43,44 → no_depth = 500
    assert result == pytest.approx(0.0)


# ── fetch_orderbook integration test ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_orderbook_returns_obi_key():
    """fetch_orderbook() includes 'obi' key computed from Kalshi YES/NO depth."""
    from bot_market import fetch_orderbook

    ob_data = {
        "orderbook": {
            "yes": [[60, 10], [65, 5]],    # yes_depth = 15
            "no":  [[40, 20], [45, 10]],   # no_depth  = 30
        }
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=ob_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    fake_headers = {
        "KALSHI-ACCESS-KEY":       "test_key",
        "KALSHI-ACCESS-TIMESTAMP": "1000000000",
        "KALSHI-ACCESS-SIGNATURE": "fakesig",
    }
    with patch("bot_market.kalshi_headers", return_value=fake_headers):
        ob = await fetch_orderbook(mock_session, "KXBTC-25MAY15-T95000", {})

    assert ob is not None, "fetch_orderbook returned None — check AMM fallback logic"
    assert "obi" in ob
    expected_obi = (30 - 15) / (30 + 15)   # = 1/3
    assert abs(ob["obi"] - expected_obi) < 1e-9


# ── _s2_obi_gate tests ────────────────────────────────────────────────────────

def test_s2_obi_gate_bullish_ticker():
    """Positive OBI (no-heavy) confirms YES trade when above min_obi threshold."""
    import bot_state
    from bot_strategy import _s2_obi_gate
    ticker = "KXBTC-25MAY15-T95000"
    bot_state._ticker_obi[ticker] = 0.5
    confirmed, val = _s2_obi_gate(ticker, "yes", 0.20)
    assert confirmed is True
    assert val == pytest.approx(0.5)


def test_s2_obi_gate_bearish_ticker():
    """Positive OBI (bullish for YES) blocks NO trade — market leans wrong way."""
    import bot_state
    from bot_strategy import _s2_obi_gate
    ticker = "KXBTC-25MAY15-T95000"
    bot_state._ticker_obi[ticker] = 0.5   # bullish; blocks NO
    confirmed, val = _s2_obi_gate(ticker, "no", 0.20)
    assert confirmed is False
    assert val == pytest.approx(0.5)


def test_s2_obi_gate_none_fails_open():
    """Missing ticker in _ticker_obi → gate fails open (True, None)."""
    from bot_strategy import _s2_obi_gate
    ticker = "KXBTC-25MAY15-T99999"
    confirmed, val = _s2_obi_gate(ticker, "yes", 0.20)
    assert confirmed is True
    assert val is None
