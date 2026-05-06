"""Tests for ticker parsing and notification-context helpers."""
from bot_risk import _parse_strike_from_ticker
from bot_infra import _notify_ctx, _phase_for_eth


def test_parse_strike_btc_ticker():
    assert _parse_strike_from_ticker("KXBTCD-26APR23-17:00-T94000") == 94000


def test_parse_strike_eth_ticker():
    assert _parse_strike_from_ticker("KXETHD-26APR23-17:00-T3500") == 3500


def test_parse_strike_15m_ticker():
    assert _parse_strike_from_ticker("KXBTC15M-26APR231715-95000") == 95000


def test_parse_strike_no_match_returns_none():
    assert _parse_strike_from_ticker("GIBBERISH-TICKER") is None
    assert _parse_strike_from_ticker("") is None
    assert _parse_strike_from_ticker(None) is None


def test_notify_ctx_15m_no_phase():
    got = _notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0)
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_15m_ignores_phase():
    got = _notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0, phase="Dwell")
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_phase_for_eth_mid():
    assert _phase_for_eth("ETH", elapsed_seconds=600.0) == "Mid"


def test_phase_for_eth_dwell():
    assert _phase_for_eth("ETH", elapsed_seconds=35 * 60) == "Dwell"


def test_phase_for_eth_late():
    assert _phase_for_eth("ETH", elapsed_seconds=50 * 60) == "Late"


def test_phase_for_eth_between():
    assert _phase_for_eth("ETH", elapsed_seconds=20 * 60) is None


def test_phase_for_eth_non_eth_returns_none():
    assert _phase_for_eth("BTC", elapsed_seconds=35 * 60) is None
