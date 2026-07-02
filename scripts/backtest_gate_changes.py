"""
scripts/backtest_gate_changes.py
Simulate effect of new filter gates on historical trades.
Does NOT replay strategy - only checks which existing trades new gates would block.

Usage:
    python scripts/backtest_gate_changes.py <path_to_trades.csv>
"""
import csv, json, sys
from collections import defaultdict


def simulate_gates(rows):
    """Apply new gates to historical rows. Returns (kept, blocked) lists."""
    kept, blocked = [], []
    for r in rows:
        if r.get("brain") != "s1":
            kept.append(r)
            continue
        try:
            sigs  = json.loads(r["entry_signals"])
            ev    = float(sigs.get("ev", 0))
            side  = r["side"]
            ep    = int(r["entry_price_cents"])
            asset = r["asset"]
            ts_hour_utc = int(r["ts"][11:13])
        except Exception:
            kept.append(r)
            continue

        # Gate: quiet hours (17 ET to 9 ET = 21 UTC to 13 UTC, summer ET=UTC-4)
        hour_et = (ts_hour_utc - 4) % 24
        if hour_et >= 17 or hour_et < 9:
            blocked.append((r, "quiet_hours"))
            continue

        # Gate: XRP disabled in S1
        if asset == "XRP":
            blocked.append((r, "xrp_disabled"))
            continue

        # Gate: min_ev 0.15
        if ev < 0.15:
            blocked.append((r, "min_ev_0.15"))
            continue

        # Gate: NO max 37c
        if side == "no" and ep > 37:
            blocked.append((r, "no_price_cap_37c"))
            continue

        kept.append(r)
    return kept, blocked


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python scripts/backtest_gate_changes.py <trades.csv>")
        sys.exit(1)

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    kept, blocked = simulate_gates(rows)

    original_pnl = sum(float(r["pnl_dollars"]) for r in rows)
    kept_pnl     = sum(float(r["pnl_dollars"]) for r in kept)
    blocked_pnl  = sum(float(r[0]["pnl_dollars"]) for r in blocked)
    saved        = -blocked_pnl

    print("=" * 60)
    print("GATE SIMULATION RESULTS")
    print("=" * 60)
    print(f"Original : {len(rows):>4} trades, PnL = ${original_pnl:>8.2f}")
    print(f"Kept     : {len(kept):>4} trades, PnL = ${kept_pnl:>8.2f}")
    print(f"Blocked  : {len(blocked):>4} trades, PnL = ${blocked_pnl:>8.2f}  (saved ${saved:.2f})")
    print()

    by_gate: dict = defaultdict(lambda: [0, 0.0])
    for r, gate in blocked:
        by_gate[gate][0] += 1
        by_gate[gate][1] += float(r["pnl_dollars"])

    print("BLOCKED BY GATE (sorted by PnL impact):")
    for gate, (count, pnl) in sorted(by_gate.items(), key=lambda x: x[1][1]):
        print(f"  {gate:<25}: {count:>4} trades, would-be PnL ${pnl:>8.2f}  (saved ${-pnl:.2f})")

    if kept:
        wins = sum(1 for r in kept if r["outcome"] == "win")
        print(f"\nKept WR: {wins}/{len(kept)} = {wins/len(kept)*100:.1f}%")

    by_asset: dict = defaultdict(lambda: [0, 0, 0.0])
    for r in kept:
        a = r["asset"]
        by_asset[a][0] += 1
        by_asset[a][1] += (1 if r["outcome"] == "win" else 0)
        by_asset[a][2] += float(r["pnl_dollars"])

    print("\nKEPT BY ASSET:")
    for a, (t, w, pnl) in sorted(by_asset.items()):
        print(f"  {a}: {t:>4} trades, WR={w/t*100:.1f}%, PnL=${pnl:.2f}")


if __name__ == "__main__":
    main()
