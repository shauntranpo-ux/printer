#!/usr/bin/env python3
"""
analyze_brain.py — Parse brain.log and print strategy analytics.

Usage:
    python scripts/analyze_brain.py [brain.log path]
    python scripts/analyze_brain.py  # auto-finds brain.log in common locations

Output:
    - Skip reason distribution (top 20)
    - EV histogram for fired S1/S2 trades
    - Realized WR from SETTLE lines
    - Trade count by hour-of-day
"""
import sys
import re
import os
from collections import Counter, defaultdict


LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else None


def _find_log() -> str:
    candidates = [
        "brain.log",
        "/data/brain.log",
        os.path.expanduser("~/brain.log"),
        os.path.join(os.path.dirname(__file__), "..", "brain.log"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    raise FileNotFoundError(
        "brain.log not found. Pass path as argument: "
        "python scripts/analyze_brain.py /path/to/brain.log"
    )


def main() -> None:
    path = LOG_PATH or _find_log()
    print(f"Reading: {path}\n")

    skip_reasons: Counter = Counter()
    ev_values: list = []
    wp_values: list = []
    hour_counts: Counter = Counter()
    settle_outcomes: list = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Skip reason: lines containing s1_/s2_ gate codes but not TRADE/SETTLE/DISLOC
            if "TRADE" not in line and "SETTLE" not in line and "DISLOC" not in line:
                m = re.search(r"\b(s[12]_\w+)", line)
                if m:
                    # Strip trailing colon-prefixed detail (e.g: s1_time_gate:4.2min → s1_time_gate)
                    reason_key = m.group(1).split(":")[0]
                    skip_reasons[reason_key] += 1

            # S1/S2 TRADE lines: "S1 TRADE ETH KXETH... ev=0.232 wp=0.75 ..."
            if ("S1 TRADE" in line or "S2 TRADE" in line) and "DISLOC" not in line:
                ev_m  = re.search(r"\bev=([-\d.]+)", line)
                wp_m  = re.search(r"\bwp=([\d.]+)", line)
                hr_m  = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2})", line)
                if ev_m:
                    ev_values.append(float(ev_m.group(1)))
                if wp_m:
                    wp_values.append(float(wp_m.group(1)))
                if hr_m:
                    hour_counts[int(hr_m.group(2))] += 1

            # SETTLE lines: "S1 SETTLE ETH KXETH... outcome=win pnl=5.50 rolling_wr=0.61 n=20"
            if "SETTLE" in line:
                out_m = re.search(r"\boutcome=(\w+)", line)
                wr_m  = re.search(r"\brolling_wr=([\d.]+)", line)
                n_m   = re.search(r"\bn=(\d+)", line)
                if out_m:
                    settle_outcomes.append((
                        out_m.group(1),
                        float(wr_m.group(1)) if wr_m else None,
                        int(n_m.group(1)) if n_m else 0,
                    ))

    # --- Skip Reason Distribution ---
    print("=" * 55)
    print("SKIP REASON DISTRIBUTION (top 20)")
    print("=" * 55)
    if skip_reasons:
        for reason, count in skip_reasons.most_common(20):
            bar = "█" * min(count // max(1, skip_reasons.most_common(1)[0][1] // 30), 30)
            print(f"  {reason:<42} {count:>6}  {bar}")
    else:
        print("  (no skip reasons found)")

    # --- EV Histogram ---
    print(f"\n{'=' * 55}")
    total_trades = len(ev_values)
    print(f"EV DISTRIBUTION (fired trades, n={total_trades})")
    print("=" * 55)
    if ev_values:
        thresholds = list(range(-10, 45, 5))
        for lo in thresholds:
            hi = lo + 5
            count = sum(1 for e in ev_values if lo / 100 <= e < hi / 100)
            if count > 0:
                bar = "█" * min(count * 2, 40)
                print(f"  EV {lo/100:.2f}–{hi/100:.2f}  {count:>5}  {bar}")
        print(f"\n  Mean EV:    {sum(ev_values) / len(ev_values):.4f}")
        sorted_ev = sorted(ev_values)
        print(f"  Median EV:  {sorted_ev[len(sorted_ev) // 2]:.4f}")
        print(f"  Min / Max:  {min(ev_values):.4f} / {max(ev_values):.4f}")
    else:
        print("  (no TRADE lines found)")

    if wp_values:
        print(f"\n  Mean WP:    {sum(wp_values) / len(wp_values):.3f}")
        print(f"  WP range:   {min(wp_values):.3f} – {max(wp_values):.3f}")

    # --- Realized WR ---
    print(f"\n{'=' * 55}")
    print(f"REALIZED WIN RATE (from SETTLE lines, n={len(settle_outcomes)})")
    print("=" * 55)
    if settle_outcomes:
        wins  = sum(1 for o, _, _ in settle_outcomes if o == "win")
        total = len(settle_outcomes)
        overall_wr = wins / total
        print(f"  Overall WR:   {wins}/{total} = {overall_wr:.1%}")
        last_wr = next((wr for _, wr, _ in reversed(settle_outcomes) if wr is not None), None)
        if last_wr is not None:
            print(f"  Rolling WR (last reported): {last_wr:.1%}")
    else:
        print("  (no SETTLE lines found)")

    # --- Trades by Hour (ET approximate) ---
    print(f"\n{'=' * 55}")
    print("TRADES BY HOUR (UTC — subtract 4 for ET)")
    print("=" * 55)
    if hour_counts:
        max_count = max(hour_counts.values())
        for hour in sorted(hour_counts.keys()):
            bar = "█" * min(int(hour_counts[hour] / max_count * 30), 30)
            print(f"  {hour:02d}:00  {hour_counts[hour]:>5}  {bar}")
    else:
        print("  (no trade timestamps found)")

    print("\nDone.")


if __name__ == "__main__":
    main()
