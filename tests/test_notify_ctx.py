"""Tests for ticker parsing and notification-context helpers in bot.py."""
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "kalshi_bot_module",
    Path(__file__).resolve().parents[1] / "bot.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["kalshi_bot_module"] = _MOD
_SPEC.loader.exec_module(_MOD)


def test_parse_strike_btc_ticker():
    assert _MOD._parse_strike_from_ticker("KXBTCD-26APR23-17:00-T94000") == 94000


def test_parse_strike_eth_ticker():
    assert _MOD._parse_strike_from_ticker("KXETHD-26APR23-17:00-T3500") == 3500


def test_parse_strike_15m_ticker():
    assert _MOD._parse_strike_from_ticker("KXBTC15M-26APR231715-95000") == 95000


def test_parse_strike_no_match_returns_none():
    assert _MOD._parse_strike_from_ticker("GIBBERISH-TICKER") is None
    assert _MOD._parse_strike_from_ticker("") is None
    assert _MOD._parse_strike_from_ticker(None) is None


def test_notify_ctx_15m_no_phase():
    got = _MOD._notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0)
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_hourly_with_phase():
    got = _MOD._notify_ctx("ETH", "KXETHD-26APR23-17:00-T3500", duration_min=60.0, phase="Dwell")
    assert got == "[ETH | hourly | KXETHD-26APR23-17:00-T3500 | Dwell]"


def test_notify_ctx_hourly_no_phase():
    got = _MOD._notify_ctx("BTC", "KXBTCD-26APR23-17:00-T94000", duration_min=60.0)
    assert got == "[BTC | hourly | KXBTCD-26APR23-17:00-T94000]"


def test_notify_ctx_15m_ignores_phase():
    got = _MOD._notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0, phase="Dwell")
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_threshold_boundary():
    got_15m = _MOD._notify_ctx("ETH", "T", duration_min=25.0)
    got_hourly = _MOD._notify_ctx("ETH", "T", duration_min=25.01)
    assert "| 15m |" in got_15m
    assert "| hourly |" in got_hourly


def test_phase_for_eth_mid():
    assert _MOD._phase_for_eth("ETH", elapsed_seconds=600.0) == "Mid"  # 10 min


def test_phase_for_eth_dwell():
    assert _MOD._phase_for_eth("ETH", elapsed_seconds=35 * 60) == "Dwell"


def test_phase_for_eth_late():
    assert _MOD._phase_for_eth("ETH", elapsed_seconds=50 * 60) == "Late"


def test_phase_for_eth_between():
    assert _MOD._phase_for_eth("ETH", elapsed_seconds=20 * 60) is None


def test_phase_for_eth_non_eth_returns_none():
    assert _MOD._phase_for_eth("BTC", elapsed_seconds=35 * 60) is None
