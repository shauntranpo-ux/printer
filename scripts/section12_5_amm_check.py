"""
Section 12.5: AMM price sanity check.

Produces simulated Kalshi orderbooks for a range of scenarios and
prints them in a table. Compare to intuition:

- At strike, 15 min remaining, 2% vol: YES ask should be ~50c
- 0.5% above strike, 5 min remaining, 1% vol: YES ask should be ~75-85c
- 1% above strike, 1 min remaining, 1% vol: YES ask should be ~90c+

If the simulator produces wildly different numbers, the AMM model is
broken and every backtest result is polluted.
"""

from __future__ import annotations
import sys

sys.path.insert(0, "src")

from strategies.backtest.kalshi_amm import simulate_orderbook


SCENARIOS = [
    # (label, current_price, strike, seconds_left, rv, asset, expected_range_yes_ask)
    ("At strike, 15min, low vol",
     100000.0, 100000.0, 900, 0.001, "BTC", (48, 55)),
    ("At strike, 15min, high vol",
     100000.0, 100000.0, 900, 0.005, "BTC", (48, 55)),
    ("0.5% above, 5min, med vol",
     100500.0, 100000.0, 300, 0.002, "BTC", (75, 88)),
    ("1% above, 1min, med vol",
     101000.0, 100000.0, 60, 0.002, "BTC", (92, 99)),
    ("0.5% below, 5min, med vol",
     99500.0, 100000.0, 300, 0.002, "BTC", (12, 25)),
    ("1% below, 1min, med vol",
     99000.0, 100000.0, 60, 0.002, "BTC", (1, 8)),
    ("ETH at strike",
     2500.0, 2500.0, 900, 0.003, "ETH", (48, 55)),
    ("ETH 1% above, 5min",
     2525.0, 2500.0, 300, 0.003, "ETH", (68, 82)),
    ("SOL at strike",
     150.0, 150.0, 900, 0.004, "SOL", (47, 55)),
    ("XRP at strike",
     2.50, 2.50, 900, 0.003, "XRP", (47, 55)),
    ("DOGE at strike",
     0.10, 0.10, 900, 0.005, "DOGE", (47, 55)),
]


def main():
    print(f"{'Scenario':<35} {'YA':>5} {'YB':>5} {'NA':>5} {'NB':>5} "
          f"{'Spread':>7} {'Expected':>12} {'OK':>3}")
    print("-" * 105)

    failures = 0
    for label, price, strike, secs, rv, asset, expected in SCENARIOS:
        ob = simulate_orderbook(price, strike, secs, rv, asset, seed=1)
        spread = ob.yes_ask - ob.yes_bid
        expected_str = f"{expected[0]}-{expected[1]}"
        ok = expected[0] <= ob.yes_ask <= expected[1]
        ok_str = "OK" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{label:<35} {ob.yes_ask:>5.1f} {ob.yes_bid:>5.1f} "
              f"{ob.no_ask:>5.1f} {ob.no_bid:>5.1f} "
              f"{spread:>7.2f} {expected_str:>12} {ok_str:>5}")

    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios within expected range")
    if failures > 0:
        print(f"WARNING: {failures} AMM scenarios produced unexpected prices — investigate")
    else:
        print("AMM prices look reasonable.")


if __name__ == "__main__":
    main()
