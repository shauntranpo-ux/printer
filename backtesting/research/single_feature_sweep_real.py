"""
Step 2 — Single-feature threshold sweep on real OHLCV data.

Key finding from step 1: V2/V3/V4/V5 have negative IC with trend-following
vote directions. 15m Kalshi markets are mean-reverting. This sweep tests
INVERTED vote directions and finds per-asset optimal thresholds.

Outputs:
  - Per-asset IC vs threshold curve for MTF momentum
  - Optimal threshold per asset
  - Updated _MTF_THRESHOLDS recommendation

Usage:
    py backtesting/research/single_feature_sweep_real.py
"""
from __future__ import annotations

import math
import os
import sys

import pandas as pd
import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
for _p in [_PROJECT_ROOT, _SRC_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtesting.data.loaders import load_bars
from strategies.signals.black_scholes import compute_bs_p_yes

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
HISTORY_BARS = 60
SECONDS_LEFT = 600.0
WINDOW_MIN = 15

# MTF threshold candidates to sweep (range covers both sides of synthetic values)
MTF_CANDIDATES = [0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003, 0.0035, 0.004,
                  0.0045, 0.005, 0.006, 0.007, 0.008, 0.010]

RSI_CANDIDATES  = [5.0, 8.0, 10.0, 12.0, 15.0, 18.0, 20.0, 25.0]
BOLL_CANDIDATES = [0.25, 0.35, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5]


def _rsi(prices: list, period: int = 14):
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _boll_z(prices: list, period: int = 20):
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean = sum(recent) / len(recent)
    var = sum((p - mean) ** 2 for p in recent) / (len(recent) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 0:
        return None
    return (prices[-1] - mean) / std


def _mtf_mom(prices: list):
    if len(prices) < 31:
        return None
    cur = prices[-1]
    if cur <= 0:
        return None
    r5  = (cur - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (cur - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (cur - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


def _vol_1min(prices: list) -> float:
    if len(prices) < 2:
        return 0.0
    log_rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices))
                if prices[i] > 0 and prices[i-1] > 0]
    if len(log_rets) < 2:
        return 0.0
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var)


def build_feature_df(asset: str) -> pd.DataFrame:
    """Load bars and compute raw signal values for every 15m window."""
    print(f"[{asset}] Loading...")
    bars = load_bars(asset, check_min_history=False)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    closes = bars["close"].values

    rows = []
    for i in range(HISTORY_BARS, len(closes) - WINDOW_MIN):
        hist   = list(closes[i - HISTORY_BARS:i])
        strike = closes[i]
        wopen  = closes[i]
        wclose = closes[i + WINDOW_MIN - 1]
        if wopen <= 0:
            continue
        label = 1 if wclose > wopen else 0

        vol     = _vol_1min(hist)
        current = hist[-1]
        bs_p    = compute_bs_p_yes(current, strike, vol, SECONDS_LEFT)
        mtf     = _mtf_mom(hist)
        rsi_v   = _rsi(hist)
        rsi_dev = (float(rsi_v) - 50.0) if rsi_v is not None else None
        boll    = _boll_z(hist)

        rows.append({
            "label":   label,
            "bs_p":    bs_p if bs_p is not None else 0.5,
            "mtf":     float(mtf) if mtf is not None else 0.0,
            "rsi_dev": float(rsi_dev) if rsi_dev is not None else 0.0,
            "boll_z":  float(boll) if boll is not None else 0.0,
        })

    df = pd.DataFrame(rows)
    print(f"[{asset}] {len(df):,} windows built")
    return df


def sweep_mtf(df: pd.DataFrame, asset: str) -> tuple[float, float]:
    """Sweep MTF threshold with INVERTED directions. Returns (best_threshold, best_IC)."""
    print(f"\n  [MTF sweep - INVERTED direction]")
    best_t, best_ic = 0.003, 0.0
    for t in MTF_CANDIDATES:
        # INVERTED: mtf < -t → YES (momentum exhaustion), mtf > +t → NO
        votes = np.where(df["mtf"] < -t, 1,
                np.where(df["mtf"] >  t, -1, 0))
        fired = votes != 0
        if fired.sum() < 1000:
            continue
        ic = pd.Series(votes).corr(df["label"], method="spearman")
        sign = "+" if ic >= 0 else ""
        flag = " <-- best" if abs(ic) > abs(best_ic) else ""
        print(f"    t={t:.4f}  IC={sign}{ic:.4f}  fired={fired.sum():,}{flag}")
        if abs(ic) > abs(best_ic):
            best_ic = ic
            best_t  = t
    return best_t, best_ic


def sweep_rsi(df: pd.DataFrame, asset: str) -> tuple[float, float]:
    """Sweep RSI threshold with INVERTED direction."""
    print(f"\n  [RSI sweep - INVERTED direction]")
    best_t, best_ic = 10.0, 0.0
    for t in RSI_CANDIDATES:
        # INVERTED: rsi_dev < -t (oversold) → YES, rsi_dev > +t (overbought) → NO
        votes = np.where(df["rsi_dev"] < -t, 1,
                np.where(df["rsi_dev"] >  t, -1, 0))
        fired = votes != 0
        if fired.sum() < 1000:
            continue
        ic = pd.Series(votes).corr(df["label"], method="spearman")
        sign = "+" if ic >= 0 else ""
        flag = " <-- best" if abs(ic) > abs(best_ic) else ""
        print(f"    t={t:.1f}  IC={sign}{ic:.4f}  fired={fired.sum():,}{flag}")
        if abs(ic) > abs(best_ic):
            best_ic = ic
            best_t  = t
    return best_t, best_ic


def sweep_boll(df: pd.DataFrame, asset: str) -> tuple[float, float]:
    """Sweep Bollinger threshold with INVERTED direction."""
    print(f"\n  [Bollinger sweep - INVERTED direction]")
    best_t, best_ic = 0.5, 0.0
    for t in BOLL_CANDIDATES:
        # INVERTED: boll < -t (below band) → YES, boll > +t (above band) → NO
        votes = np.where(df["boll_z"] < -t, 1,
                np.where(df["boll_z"] >  t, -1, 0))
        fired = votes != 0
        if fired.sum() < 1000:
            continue
        ic = pd.Series(votes).corr(df["label"], method="spearman")
        sign = "+" if ic >= 0 else ""
        flag = " <-- best" if abs(ic) > abs(best_ic) else ""
        print(f"    t={t:.2f}  IC={sign}{ic:.4f}  fired={fired.sum():,}{flag}")
        if abs(ic) > abs(best_ic):
            best_ic = ic
            best_t  = t
    return best_t, best_ic


def test_inverted_ensemble(df: pd.DataFrame, mtf_t: float, rsi_t: float, boll_t: float) -> dict:
    """Test the full inverted ensemble with best thresholds."""
    v1 = np.where(df["bs_p"] > 0.5, 1, -1)
    v2 = np.where(df["mtf"] < -mtf_t, 1, np.where(df["mtf"] > mtf_t, -1, 0))
    v3 = np.where(df["rsi_dev"] < -rsi_t, 1, np.where(df["rsi_dev"] > rsi_t, -1, 0))
    v4 = np.where(df["boll_z"] < -boll_t, 1, np.where(df["boll_z"] > boll_t, -1, 0))
    v5 = np.where(df["mtf"] < -(mtf_t/2), 1, np.where(df["mtf"] > (mtf_t/2), -1, 0))

    yes_votes = (v1 == 1).astype(int) + (v2 == 1).astype(int) + (v3 == 1).astype(int) + \
                (v4 == 1).astype(int) + (v5 == 1).astype(int)
    no_votes  = (v1 == -1).astype(int) + (v2 == -1).astype(int) + (v3 == -1).astype(int) + \
                (v4 == -1).astype(int) + (v5 == -1).astype(int)

    ensemble = np.where(yes_votes >= 3, 1, np.where(no_votes >= 3, -1, 0))

    fired    = ensemble != 0
    yes_fire = ensemble == 1
    no_fire  = ensemble == -1

    n_fired = fired.sum()
    if n_fired == 0:
        return {"ic": 0.0, "fire_rate": 0.0, "yes_pct": 0.0, "wr_yes": 0.0, "wr_no": 0.0}

    ic       = pd.Series(ensemble).corr(df["label"], method="spearman")
    fire_rate = n_fired / len(df)
    yes_pct   = yes_fire.sum() / n_fired
    wr_yes    = df["label"][yes_fire].mean() if yes_fire.sum() > 0 else 0.0
    wr_no     = (1 - df["label"][no_fire].mean()) if no_fire.sum() > 0 else 0.0

    return {
        "ic":        round(float(ic), 4),
        "fire_rate": round(float(fire_rate), 3),
        "yes_pct":   round(float(yes_pct), 3),
        "wr_yes":    round(float(wr_yes), 3),
        "wr_no":     round(float(wr_no), 3),
    }


def main():
    print("=" * 60)
    print("Step 2 -- Single-Feature Threshold Sweep (Inverted)")
    print("=" * 60)

    recommendations = {}

    for asset in ASSETS:
        print(f"\n{'='*60}")
        print(f"  {asset}")
        print(f"{'='*60}")

        try:
            df = build_feature_df(asset)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        mtf_t, mtf_ic   = sweep_mtf(df, asset)
        rsi_t, rsi_ic   = sweep_rsi(df, asset)
        boll_t, boll_ic = sweep_boll(df, asset)

        ens = test_inverted_ensemble(df, mtf_t, rsi_t, boll_t)

        print(f"\n  Best thresholds for {asset}:")
        print(f"    MTF  t={mtf_t:.4f}  IC={mtf_ic:+.4f}")
        print(f"    RSI  t={rsi_t:.1f}   IC={rsi_ic:+.4f}")
        print(f"    Boll t={boll_t:.2f}  IC={boll_ic:+.4f}")
        print(f"  Inverted ensemble IC={ens['ic']:+.4f}  fire={ens['fire_rate']:.1%}  YES={ens['yes_pct']:.1%}  WR_Y={ens['wr_yes']:.1%}  WR_N={ens['wr_no']:.1%}")

        recommendations[asset] = {
            "mtf_threshold": mtf_t,
            "rsi_threshold": rsi_t,
            "boll_threshold": boll_t,
            "ensemble": ens,
        }

    print(f"\n{'='*60}")
    print("RECOMMENDATIONS")
    print(f"{'='*60}")
    print("\nUpdated _MTF_THRESHOLDS (inverted vote direction):")
    for asset, rec in recommendations.items():
        print(f"  '{asset}': {rec['mtf_threshold']},")

    print("\nPer-asset inverted ensemble performance:")
    hdr = f"{'Asset':<6} {'MTF_t':>7} {'RSI_t':>6} {'Boll_t':>7} {'EnsIC':>7} {'Fire%':>6} {'YES%':>6} {'WR_Y':>6} {'WR_N':>6}"
    print(hdr)
    print("-" * len(hdr))
    for asset, rec in recommendations.items():
        e = rec["ensemble"]
        print(
            f"{asset:<6}"
            f"{rec['mtf_threshold']:>7.4f}"
            f"{rec['rsi_threshold']:>6.1f}"
            f"{rec['boll_threshold']:>7.2f}"
            f"{e['ic']:>7.4f}"
            f"{e['fire_rate']:>6.1%}"
            f"{e['yes_pct']:>6.1%}"
            f"{e['wr_yes']:>6.1%}"
            f"{e['wr_no']:>6.1%}"
        )

    print("\nNext step: if WR_Y and WR_N both > 50%, update fifteen_min_signal.py")
    print("with inverted vote logic and new thresholds.")

    out = os.path.join(_THIS_DIR, "sweep_results_real.md")
    with open(out, "w") as f:
        f.write("# Step 2 -- Single-Feature Threshold Sweep (Inverted Directions)\n\n")
        f.write("## Optimal Thresholds\n\n")
        f.write("| Asset | MTF threshold | RSI threshold | Boll threshold | Ens IC | Fire% | YES% | WR_Y | WR_N |\n")
        f.write("|-------|--------------|--------------|----------------|--------|-------|------|------|------|\n")
        for asset, rec in recommendations.items():
            e = rec["ensemble"]
            f.write(
                f"| {asset} "
                f"| {rec['mtf_threshold']:.4f} "
                f"| {rec['rsi_threshold']:.1f} "
                f"| {rec['boll_threshold']:.2f} "
                f"| {e['ic']:.4f} "
                f"| {e['fire_rate']:.1%} "
                f"| {e['yes_pct']:.1%} "
                f"| {e['wr_yes']:.1%} "
                f"| {e['wr_no']:.1%} |\n"
            )
        f.write("\n## Updated MTF Thresholds\n\n```python\n_MTF_THRESHOLDS = {\n")
        for asset, rec in recommendations.items():
            f.write(f'    "{asset}": {rec["mtf_threshold"]},\n')
        f.write("}\n```\n")
    print(f"\nResults -> {out}")


if __name__ == "__main__":
    main()
