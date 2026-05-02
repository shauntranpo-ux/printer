import time
import pytest

from strategies.btc_strategy import BTCStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.signals.btc_diurnal_obi import kalshi_book_obi


def _btc_features(above_strike: bool = True,
                  yes_bid=58.0, no_bid=40.0, yes_ask=60.0, no_ask=42.0):
    now = time.time()
    current = 100100.0 if above_strike else 99900.0
    strike = 100000.0
    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=now,
        current_price=current,
        strike=strike,
        btc_price=current,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=yes_bid,
        no_bid=no_bid,
        spread_yes=yes_ask - yes_bid,
        spread_no=no_ask - no_bid,
        realized_vol_1min=0.001,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 5 + i * 0.1))
        f.prices_1m.append((ts, current - 5 + i * 0.1))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, yes_ask))
    return f


def test_kalshi_book_obi_directionality():
    # YES priced higher than NO → positive OBI
    obi_up = kalshi_book_obi(yes_bid_c=60, no_bid_c=38, yes_ask_c=62, no_ask_c=40)
    assert obi_up is not None and obi_up > 0
    # NO priced higher than YES → negative OBI
    obi_down = kalshi_book_obi(yes_bid_c=38, no_bid_c=60, yes_ask_c=40, no_ask_c=62)
    assert obi_down is not None and obi_down < 0
    # Symmetric book → near zero
    obi_flat = kalshi_book_obi(yes_bid_c=49, no_bid_c=49, yes_ask_c=51, no_ask_c=51)
    assert obi_flat is not None and abs(obi_flat) < 1e-6


def test_btc_decides_trade_or_skip():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    d = strat.decide(_btc_features())
    assert d.action in ("trade", "skip")


def test_btc_obi_signal_present_in_signals():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _btc_features(yes_bid=62, no_bid=37, yes_ask=64, no_ask=39)
    d = strat.decide(f)
    if d.action == "trade":
        assert "obi" in d.contributing_signals
        assert "obi_adj" in d.contributing_signals
