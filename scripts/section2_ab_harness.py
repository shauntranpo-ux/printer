"""
Section 2 A/B parity harness.

Runs the legacy printer_brain() AND the new BTCStrategy on the same
synthetic inputs and confirms they produce the same decision.

Run it manually:
    python scripts/section2_ab_harness.py
"""

import sys
import time
from collections import deque

sys.path.insert(0, ".")

import bot  # noqa: E402
from strategies.btc_strategy import BTCStrategy  # noqa: E402
from strategies.features import MarketFeatures  # noqa: E402
from strategies.skip_layer import SkipConfig  # noqa: E402


def make_bot_global_state(btc_prices_list):
    """Populate bot.btc_prices with a deque matching the new features."""
    bot.btc_prices.clear()
    for ts, p in btc_prices_list:
        bot.btc_prices.append((ts, p))


def make_features(btc_price, strike, yes_ask, no_ask, seconds_left=600):
    now = time.time()
    price_hist = [(now - (60 - i) * 60, btc_price - 100 + i * 5) for i in range(60)]
    make_bot_global_state(price_hist)

    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=now,
        current_price=btc_price,
        strike=strike,
        btc_price=btc_price,
        seconds_left=seconds_left,
        elapsed_seconds=900 - seconds_left,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=max(0.0, yes_ask - 1.0),
        no_bid=max(0.0, no_ask - 1.0),
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for ts, p in price_hist:
        f.prices_60m.append((ts, p))
        f.prices_1m.append((ts, p))
    return f


def compare_one(btc_price, strike, yes_ask, no_ask):
    features = make_features(btc_price, strike, yes_ask, no_ask)
    secs_left = features.seconds_left
    elapsed = features.elapsed_seconds

    # Legacy output
    legacy = bot.printer_brain(
        btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, "KXBTCD-TEST",
        min_ev_base=3.0,
    )

    # New output (continuation_only=True to match legacy)
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.03,
        stake_dollars=5.0,
        continuation_only=True,
    )
    new_decision = strat.decide(features)

    legacy_action = legacy["action"]
    new_action = new_decision.action
    legacy_side = legacy["side"]
    new_side = new_decision.side

    agreement = (legacy_action == new_action)
    if legacy_action == "trade" and new_action == "trade":
        agreement = agreement and (legacy_side == new_side)

    return {
        "btc_price": btc_price,
        "strike": strike,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "legacy_action": legacy_action,
        "legacy_side": legacy_side,
        "legacy_win_prob": legacy["win_prob"],
        "new_action": new_action,
        "new_side": new_side,
        "new_p_model": new_decision.p_model,
        "agreement": agreement,
    }


def main():
    scenarios = [
        (100500, 100000, 70, 32),  # above strike, wide spread
        (100200, 100000, 65, 37),  # above strike, mid
        (99500, 100000, 30, 72),   # below strike
        (99800, 100000, 45, 57),   # below strike, close
        (100100, 100000, 55, 47),  # just above, tight
    ]

    disagreements = 0
    for btc, strike, yes, no in scenarios:
        r = compare_one(btc, strike, yes, no)
        status = "AGREE" if r["agreement"] else "DIFF "
        print(
            f"{status} btc={btc} strike={strike} "
            f"yes={yes}c no={no}c | "
            f"legacy={r['legacy_action']}/{r['legacy_side']} "
            f"p={r['legacy_win_prob']:.3f} | "
            f"new={r['new_action']}/{r['new_side']} "
            f"p={r['new_p_model']:.3f}"
        )
        if not r["agreement"]:
            disagreements += 1

    print(f"\n{len(scenarios) - disagreements}/{len(scenarios)} scenarios matched")
    if disagreements > 0:
        print(
            "DISAGREEMENTS DETECTED. Investigate before enabling the "
            "flag in live trading."
        )
        sys.exit(1)
    print("Parity OK.")


if __name__ == "__main__":
    main()
