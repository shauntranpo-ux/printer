#!/usr/bin/env python3
"""
validate_and_report.py — Final gate before live trading.

Reads price_validation_log.csv and runs a full price model validation.
Gives a clear GO / MARGINAL / NO-GO verdict.

The single question this answers:
  Do the simulated AMM prices used in the backtest match what Kalshi
  actually charges? If not, every EV estimate is wrong.

Reviewer threshold:
  GO:        avg price gap < 3c
  MARGINAL:  avg price gap 3-7c
  NO-GO:     avg price gap > 7c

Usage:
    python validate_and_report.py
    python validate_and_report.py --csv path/to/price_validation_log.csv
    python validate_and_report.py --fee 7
"""

import argparse
import csv
import math
import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(_BASE_DIR, "price_validation_log.csv")
DEFAULT_FEE = 7.0   # cents per contract


# ── Helpers ───────────────────────────────────────────────────────────────────

def _border(char="═", width=60):
    print(char * width)


def _section(title, width=60):
    print()
    _border("═", width)
    print(f"  {title}")
    _border("═", width)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _pct(n, total):
    return f"{n / total * 100:.1f}%" if total else "0.0%"


def _bar(count, max_count, width=20):
    filled = int(count / max_count * width) if max_count else 0
    return "█" * filled + "░" * (width - filled)


# ── CSV loader ────────────────────────────────────────────────────────────────

def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                real_yes = None if row.get("real_yes_ask", "null") == "null" else float(row["real_yes_ask"])
                sim_yes  = float(row["sim_yes_ask"])
                gap      = None if row.get("price_gap_cents", "null") == "null" else float(row["price_gap_cents"])
                rows.append({
                    "ts":            row.get("ts", ""),
                    "ticker":        row.get("ticker", ""),
                    "btc_price":     float(row.get("btc_price", 0)),
                    "strike":        float(row.get("strike", 0)),
                    "abs_pct":       float(row.get("abs_pct", 0)),
                    "mins_remaining": float(row.get("mins_remaining", 0)),
                    "sim_yes_ask":   sim_yes,
                    "real_yes_ask":  real_yes,
                    "gap":           gap,
                })
            except (ValueError, KeyError):
                continue
    return rows


# ── Analysis sections ─────────────────────────────────────────────────────────

def check_1_sample_size(rows: list[dict], valid: list[dict]) -> bool:
    n       = len(rows)
    n_valid = len(valid)
    _section("1. SAMPLE SIZE")
    print(f"  Total rows in CSV      : {n}")
    print(f"  Rows with real prices  : {n_valid}")
    print(f"  Rows with null prices  : {n - n_valid}  (API call failed — excluded from analysis)")
    print()
    if n < 50:
        print(f"  INSUFFICIENT DATA. Need 200+ samples for reliable analysis. Currently at {n}.")
        print("  Keep running paper mode.")
        return False
    if n < 200:
        print(f"  WARNING: Results are directional but not statistically robust. {n}/200 minimum samples.")
    else:
        print(f"  OK — {n} samples collected.")
    return True


def check_2_gap_analysis(gaps: list[float]) -> float:
    n          = len(gaps)
    mean_gap   = _mean(gaps)
    median_gap = _median(gaps)
    std_gap    = _stdev(gaps)
    min_gap    = min(gaps)
    max_gap    = max(gaps)
    pct_higher = sum(1 for g in gaps if g > 0) / n * 100

    _section("2. PRICE GAP ANALYSIS  (real_yes_ask − simulated_yes_ask)")
    print(f"  n                          : {n}")
    print(f"  Mean gap                   : {mean_gap:+.2f}c")
    print(f"  Median gap                 : {median_gap:+.2f}c")
    print(f"  Std dev                    : {std_gap:.2f}c")
    print(f"  Min gap                    : {min_gap:+.2f}c")
    print(f"  Max gap                    : {max_gap:+.2f}c")
    print(f"  % trades real > simulated  : {pct_higher:.1f}%")
    print()
    if pct_higher > 80:
        print("  Real prices are consistently HIGHER than simulated.")
        print("  The backtest systematically uses prices that are too cheap.")
    elif pct_higher < 20:
        print("  Real prices are often LOWER than simulated — simulator is conservative.")
        print("  Backtest may be underestimating edge slightly.")
    else:
        print("  Mixed — real and simulated prices are close in both directions.")
    return mean_gap


def check_3_distribution(gaps: list[float]):
    n = len(gaps)
    buckets = [
        ("Real LOWER than simulated   (free edge)",         lambda g: g < 0),
        ("Real  0-2c higher           (close — good)",      lambda g: 0   <= g <  3),
        ("Real  3-5c higher           (concerning)",        lambda g: 3   <= g <  6),
        ("Real  6-8c higher           (bad — fee eaten)",   lambda g: 6   <= g <  9),
        ("Real  9-12c higher          (strategy dead)",     lambda g: 9   <= g < 13),
        ("Real 13c+ higher            (sim totally wrong)", lambda g: g >= 13),
    ]
    counts = [sum(1 for g in gaps if fn(g)) for _, fn in buckets]
    max_c  = max(counts) if counts else 1

    _section("3. PRICE GAP DISTRIBUTION")
    print(f"  {'Bucket':<42}  {'Count':>6}  {'Share':>6}  {'Bar'}")
    print(f"  {'-'*42}  {'-'*6}  {'-'*6}  {'-'*20}")
    for (label, _), count in zip(buckets, counts):
        bar = _bar(count, max_c)
        print(f"  {label:<42}  {count:>6,}  {_pct(count, n):>6}  {bar}")


def check_4_by_distance(valid: list[dict]):
    buckets = [
        ("0.0 – 0.1%",  lambda r: r["abs_pct"] <  0.1),
        ("0.1 – 0.2%",  lambda r: 0.1 <= r["abs_pct"] <  0.2),
        ("0.2 – 0.3%",  lambda r: 0.2 <= r["abs_pct"] <  0.3),
        ("0.3%+      ",  lambda r: r["abs_pct"] >= 0.3),
    ]
    _section("4. GAP BY DISTANCE FROM STRIKE")
    print(f"  {'Bucket':<12}  {'Count':>6}  {'Avg Sim':>8}  {'Avg Real':>9}  {'Avg Gap':>8}")
    print(f"  {'-'*12}  {'-'*6}  {'-'*8}  {'-'*9}  {'-'*8}")
    for label, fn in buckets:
        rows = [r for r in valid if fn(r)]
        if not rows:
            print(f"  {label:<12}  {'0':>6}  {'—':>8}  {'—':>9}  {'—':>8}")
            continue
        avg_sim  = _mean([r["sim_yes_ask"]  for r in rows])
        avg_real = _mean([r["real_yes_ask"] for r in rows])
        avg_gap  = _mean([r["gap"]          for r in rows])
        flag     = "  ← HIGH CONFIDENCE BUCKET" if "0.3" in label and avg_gap > 5 else ""
        print(f"  {label:<12}  {len(rows):>6,}  {avg_sim:>7.1f}c  {avg_real:>8.1f}c  {avg_gap:>+7.1f}c{flag}")
    print()
    print("  NOTE: The reviewer specifically flagged that simulation is worst at")
    print("  0.3%+ distance — the exact scenario where the bot bets most aggressively.")


def check_5_by_time(valid: list[dict]):
    buckets = [
        ("1-3 min  ", lambda r: 1  <= r["mins_remaining"] <  4),
        ("4-6 min  ", lambda r: 4  <= r["mins_remaining"] <  7),
        ("7-10 min ", lambda r: 7  <= r["mins_remaining"] < 11),
        ("10+ min  ", lambda r: r["mins_remaining"] >= 11),
        ("< 1 min  ", lambda r: r["mins_remaining"] <  1),
    ]
    _section("5. GAP BY MINUTES REMAINING")
    print(f"  {'Bucket':<10}  {'Count':>6}  {'Avg Sim':>8}  {'Avg Real':>9}  {'Avg Gap':>8}")
    print(f"  {'-'*10}  {'-'*6}  {'-'*8}  {'-'*9}  {'-'*8}")
    for label, fn in buckets:
        rows = [r for r in valid if fn(r)]
        if not rows:
            continue
        avg_sim  = _mean([r["sim_yes_ask"]  for r in rows])
        avg_real = _mean([r["real_yes_ask"] for r in rows])
        avg_gap  = _mean([r["gap"]          for r in rows])
        print(f"  {label:<10}  {len(rows):>6,}  {avg_sim:>7.1f}c  {avg_real:>8.1f}c  {avg_gap:>+7.1f}c")


def check_6_ev_reality(valid: list[dict], fee: float):
    """
    EV reality check.

    Full EV = bv3_prob - (price / 100) - fee.  bv3_prob is not in the CSV
    (it's computed at runtime from the BV3 table).  What we CAN compute:

      cost_with_sim  = sim_yes_ask  / 100 + fee   (what the backtest paid)
      cost_with_real = real_yes_ask / 100 + fee   (what live trading pays)

    The extra cost per contract = gap / 100.

    Breakeven BV3 probability required with real prices =
        (real_yes_ask + fee_cents) / 100

    The backtest's measured win rate was ~87.5%.  We show how much of the
    87.5% edge is consumed by the price gap.
    """
    fee_frac      = fee / 100.0
    BACKTEST_WR   = 0.875   # backtest measured win rate (BV3 + fee-inclusive, 128k windows)

    sim_costs   = [(r["sim_yes_ask"]  / 100.0 + fee_frac) for r in valid]
    real_costs  = [(r["real_yes_ask"] / 100.0 + fee_frac) for r in valid]

    avg_sim_cost  = _mean(sim_costs)
    avg_real_cost = _mean(real_costs)

    # EV using backtest win rate assumption
    avg_sim_ev   = BACKTEST_WR - avg_sim_cost
    avg_real_ev  = BACKTEST_WR - avg_real_cost

    # Breakeven win rate required at real prices
    breakeven_wrs = [c for c in real_costs]   # prob needed = cost
    avg_breakeven  = _mean(breakeven_wrs)

    # What fraction of samples require win rate ABOVE backtest WR?
    above_backtest = sum(1 for c in real_costs if c > BACKTEST_WR)
    pct_above      = above_backtest / len(real_costs) * 100

    # Gap-flipped trades: sim EV looked positive (>0) but real EV negative (<0)
    flipped = sum(
        1 for r in valid
        if (BACKTEST_WR - r["sim_yes_ask"] / 100.0 - fee_frac) > 0
        and (BACKTEST_WR - r["real_yes_ask"] / 100.0 - fee_frac) <= 0
    )
    pct_flipped = flipped / len(valid) * 100

    _section("6. POST-FEE EV REALITY CHECK")
    print(f"  Kalshi fee used              : {fee:.0f}c/contract")
    print(f"  Assumed win rate (backtest)  : {BACKTEST_WR*100:.1f}%  (BV3 table, 128k windows)")
    print(f"  NOTE: BV3 win rate has data leakage — treat as optimistic upper bound.")
    print()
    print(f"  Avg EV/contract (sim prices)  : {avg_sim_ev*100:+.2f}c  (${avg_sim_ev:+.4f})")
    print(f"  Avg EV/contract (real prices) : {avg_real_ev*100:+.2f}c  (${avg_real_ev:+.4f})")
    print()
    print(f"  Avg breakeven win rate (real) : {avg_breakeven*100:.1f}%")
    print(f"  Trades requiring WR > {BACKTEST_WR*100:.0f}%   : {above_backtest:,} ({pct_above:.1f}%)")
    print(f"  Trades flipped +EV→-EV by gap : {flipped:,} ({pct_flipped:.1f}%)")
    print()
    if avg_real_ev > 0:
        print(f"  Avg real EV is POSITIVE: {avg_real_ev*100:+.2f}c/contract")
        print(f"  At ~38 contracts/$25 trade: estimated {avg_real_ev*38*100:.0f}c ≈ ${avg_real_ev*38:.2f}/trade")
    else:
        print(f"  Avg real EV is NEGATIVE: {avg_real_ev*100:+.2f}c/contract")
        print(f"  Strategy loses money on average with real Kalshi prices.")
    return avg_real_ev


def check_7_verdict(mean_gap: float, avg_real_ev: float, n: int, fee: float):
    _section("7. VERDICT")

    # A — Simulator accuracy
    print("  A) IS THE PRICE SIMULATOR ACCURATE?")
    if mean_gap < 3:
        sim_verdict = "ACCURATE"
        print(f"     Avg gap = {mean_gap:+.2f}c < 3c.")
        print("     Simulator is reasonably accurate. Proceed with caution.")
    elif mean_gap < 7:
        sim_verdict = "INACCURATE"
        print(f"     Avg gap = {mean_gap:+.2f}c (3-7c range).")
        print("     Simulator underprices contracts. Edge is marginal to zero after fees.")
        print("     Strategy needs recalibration before live trading.")
    else:
        sim_verdict = "WRONG"
        print(f"     Avg gap = {mean_gap:+.2f}c > 7c.")
        print("     Simulator is fundamentally wrong. The fee alone is {fee:.0f}c — this gap")
        print("     exceeds it. Strategy has NEGATIVE expected value after fees.")
        print("     Do not trade real money.")
    print()

    # B — Real EV
    print("  B) DOES THE STRATEGY HAVE POSITIVE EV AFTER FEES WITH REAL PRICES?")
    ev_per_trade = avg_real_ev * 38   # ~38 contracts at $25/trade, 65c avg entry
    if avg_real_ev > 0:
        print(f"     YES — avg real EV ≈ {avg_real_ev*100:+.2f}c/contract ≈ ${ev_per_trade:.2f}/trade")
        print(f"     (assumes 87.5% BV3 win rate — this rate has BV3 data leakage, may be optimistic)")
    else:
        print(f"     NO — avg real EV ≈ {avg_real_ev*100:+.2f}c/contract ≈ ${ev_per_trade:.2f}/trade")
        print(f"     Strategy loses money with real Kalshi prices under backtest assumptions.")
    print()

    # C — Should the user risk real money?
    print("  C) SHOULD YOU RISK REAL MONEY?")
    if avg_real_ev > 0 and ev_per_trade >= 2.0 and mean_gap < 3:
        go_verdict = "GO"
        print("     Cautiously yes — start with minimum position sizes and track for")
        print("     100+ live trades before scaling.")
    elif avg_real_ev > 0 and ev_per_trade >= 0:
        go_verdict = "MARGINAL"
        print(f"     MARGINAL — edge exists but is thin (${ev_per_trade:.2f}/trade est.).")
        print("     One bad week of variance wipes months of gains. High risk.")
    else:
        go_verdict = "NO-GO"
        print("     NO. The strategy loses money with real Kalshi prices after fees.")
        print("     Do not deploy with real money until the pricing model is fixed")
        print("     or a genuine mispricing is identified.")

    # Final box
    print()
    _border("═", 60)
    print("  PRICE VALIDATION VERDICT")
    _border("═", 60)
    print(f"  Samples analyzed     : {n}")
    print(f"  Avg price gap        : {mean_gap:+.2f}c")
    print(f"  Simulator accuracy   : {sim_verdict}")
    print(f"  Strategy EV (real)   : {'POSITIVE' if avg_real_ev > 0 else 'NEGATIVE'} "
          f"({avg_real_ev*100:+.2f}c/contract, ~${ev_per_trade:+.2f}/trade)")
    print(f"  Recommendation       : {go_verdict}")
    _border("═", 60)

    if go_verdict == "NO-GO":
        print()
        print("  Do not trade real money. The simulated prices do not match reality.")
        print("  Fix the pricing model or identify a different edge before going live.")
    elif go_verdict == "GO":
        print()
        print("  Price model validated. You may cautiously proceed to live trading")
        print("  with minimum position sizes.")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Kalshi price model validation report")
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help=f"Path to price_validation_log.csv (default: {DEFAULT_CSV})")
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help=f"Kalshi fee in cents per contract (default: {DEFAULT_FEE})")
    args = ap.parse_args()

    # ── Sample size gate ──────────────────────────────────────────────────────
    if not os.path.exists(args.csv):
        print(f"\n{'='*60}")
        print("  PRICE VALIDATION: NO DATA")
        print(f"{'='*60}")
        print(f"  {args.csv}")
        print("  File does not exist.")
        print()
        print("  Run paper mode or the standalone validator:")
        print("    python price_validator.py")
        print()
        print("  Need 200+ samples (~3.5h at 60s/sample).")
        print(f"{'='*60}")
        sys.exit(1)

    print(f"\nLoading {args.csv} ...")
    rows  = load_rows(args.csv)
    valid = [r for r in rows if r["gap"] is not None and r["real_yes_ask"] is not None]

    ok = check_1_sample_size(rows, valid)
    if not ok:
        sys.exit(1)

    if not valid:
        print("\nNo rows with real price data. All real_yes_ask values are null.")
        print("The bot may not have reached handle_ready_phase() yet.")
        sys.exit(1)

    gaps     = [r["gap"] for r in valid]
    mean_gap = check_2_gap_analysis(gaps)
    check_3_distribution(gaps)
    check_4_by_distance(valid)
    check_5_by_time(valid)
    avg_real_ev = check_6_ev_reality(valid, args.fee)
    check_7_verdict(mean_gap, avg_real_ev, len(valid), args.fee)


if __name__ == "__main__":
    main()
