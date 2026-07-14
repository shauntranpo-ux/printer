"""
scripts/plot_report.py - render P&L / calibration / sigma charts from the trades
table or a CSV export.

    python3 scripts/plot_report.py --csv kalshi_trades_export.csv --out results/
    python3 scripts/plot_report.py --db kalshi_bot.db --out results/

Writes four PNGs: equity_curve, calibration, pnl_by_entry_bucket, sigma_check.
matplotlib is a script-only dependency (not in requirements.txt); install with
`pip install matplotlib` if missing.
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from statistics import NormalDist

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("pip install matplotlib to render graphs")
    sys.exit(1)

ND = NormalDist()

COLUMNS = ("id", "ts", "mode", "side", "entry_price_cents", "outcome", "pnl_dollars",
           "btc_price_at_entry", "strike", "seconds_left_at_entry", "raw_p_yes",
           "entry_signals", "asset", "brain", "model_prob")


def load_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_db(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM trades WHERE outcome IN ('win','loss') ORDER BY ts")]
    finally:
        con.close()
    return rows


def prep(rows):
    out = []
    for r in rows:
        if r.get("outcome") not in ("win", "loss"):
            continue   # open/unfilled rows would chart as phantom losses
        try:
            sig = json.loads(r.get("entry_signals") or "{}")
        except Exception:
            sig = {}
        try:
            t = dict(
                ts=r.get("ts") or "",
                asset=r.get("asset") or "?",
                side=r.get("side") or "?",
                brain=r.get("brain") or r.get("strategy_variant") or "?",
                entry=float(r.get("entry_price_cents") or 0),
                pnl=float(r.get("pnl_dollars") or 0),
                win=1 if r.get("outcome") == "win" else 0,
                spot=float(r.get("btc_price_at_entry") or 0),
                strike=float(r.get("strike") or 0),
                secs=float(r.get("seconds_left_at_entry") or 0),
                sig=sig,
            )
        except (TypeError, ValueError):
            continue
        t["mkt_p"] = sig.get("mkt_p")
        t["model_p"] = sig.get("win_prob", _f(r.get("model_prob")))
        out.append(t)
    out.sort(key=lambda t: t["ts"])
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def plot_equity(trades, out):
    fig, ax = plt.subplots(figsize=(10, 5))
    for label, sel in (("total", trades),
                       ("s1", [t for t in trades if t["brain"] == "s1"]),
                       ("s2", [t for t in trades if t["brain"] == "s2"])):
        if not sel:
            continue
        xs = list(range(1, len(sel) + 1))
        cum, ys = 0.0, []
        for t in sel:
            cum += t["pnl"]
            ys.append(cum)
        ax.plot(xs, ys, label=f"{label} (${cum:+.0f})", linewidth=2 if label == "total" else 1)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_xlabel("trade #")
    ax.set_ylabel("cumulative P&L ($)")
    ax.set_title("Equity curve")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out, "equity_curve.png"), dpi=130)
    plt.close(fig)


def plot_calibration(trades, out):
    """Model probability vs market probability vs realized, bucketed by market p."""
    pts = [t for t in trades if t["mkt_p"] is not None and t["model_p"] is not None]
    buckets = [(0.0, 0.12), (0.12, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 1.0)]
    xs, model_y, mkt_y, real_y, ns = [], [], [], [], []
    for lo, hi in buckets:
        sel = [t for t in pts if lo <= t["mkt_p"] < hi]
        if not sel:
            continue
        xs.append((lo + hi) / 2)
        model_y.append(sum(t["model_p"] for t in sel) / len(sel))
        mkt_y.append(sum(t["mkt_p"] for t in sel) / len(sel))
        real_y.append(sum(t["win"] for t in sel) / len(sel))
        ns.append(len(sel))
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot([0, 1], [0, 1], "--", color="gray", linewidth=0.8, label="perfect calibration")
    ax.plot(mkt_y, real_y, "o-", label="market price vs realized")
    ax.plot(mkt_y, model_y, "s-", label="model prob (same trades)")
    for x, y, n in zip(mkt_y, real_y, ns):
        ax.annotate(f"n={n}", (x, y), textcoords="offset points", xytext=(6, -10), fontsize=8)
    ax.set_xlabel("market-implied probability of our side")
    ax.set_ylabel("probability")
    ax.set_title("Calibration: the market was right, the model was not")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out, "calibration.png"), dpi=130)
    plt.close(fig)


def plot_entry_buckets(trades, out):
    buckets = [(0, 15), (15, 25), (25, 45), (45, 65), (65, 85), (85, 101)]
    labels, pnls, wrs = [], [], []
    for lo, hi in buckets:
        sel = [t for t in trades if lo <= t["entry"] < hi]
        if not sel:
            continue
        labels.append(f"{lo}-{hi}c\nn={len(sel)}")
        pnls.append(sum(t["pnl"] for t in sel))
        wrs.append(100.0 * sum(t["win"] for t in sel) / len(sel))
    fig, ax = plt.subplots(figsize=(9, 5))
    colors = ["#c0392b" if p < 0 else "#27ae60" for p in pnls]
    bars = ax.bar(labels, pnls, color=colors)
    for b, wr in zip(bars, wrs):
        ax.annotate(f"{wr:.0f}% win", (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom" if b.get_height() >= 0 else "top", fontsize=9)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_ylabel("P&L ($)")
    ax.set_title("P&L by entry price: the cheap tails did the damage")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "pnl_by_entry_bucket.png"), dpi=130)
    plt.close(fig)


def plot_sigma_check(trades, out):
    """Sigma the model used vs the sigma implied by the market's own quote at entry."""
    fig, ax = plt.subplots(figsize=(9, 5))
    assets = sorted({t["asset"] for t in trades})
    markers = {"BTC": "o", "ETH": "^", "SOL": "s", "XRP": "D", "DOGE": "v"}
    plotted = False
    for asset in assets:
        used_pts, imp_pts = [], []
        for i, t in enumerate(trades):
            if t["asset"] != asset:
                continue
            used = t["sig"].get("sigma_eff")
            if t["mkt_p"] is None or not t["spot"] or not t["strike"] or not t["secs"]:
                continue
            p_yes = t["mkt_p"] if t["side"] == "yes" else 1 - t["mkt_p"]
            p_yes = min(max(p_yes, 0.01), 0.99)
            z = ND.inv_cdf(p_yes)
            if abs(z) < 0.1:
                continue
            ps = math.log(t["spot"] / t["strike"]) / z
            if ps <= 0:
                continue
            implied = ps / math.sqrt(max(1.0 / 900.0, t["secs"] / 900.0))
            imp_pts.append((i, implied * 100))
            if used:
                used_pts.append((i, used * 100))
            plotted = True
        m = markers.get(asset, "o")
        if imp_pts:
            ax.scatter(*zip(*imp_pts), marker=m, s=28, label=f"{asset} market-implied")
        if used_pts:
            ax.scatter(*zip(*used_pts), marker=m, s=28, facecolors="none",
                       edgecolors="red", label=f"{asset} model used")
    if not plotted:
        plt.close(fig)
        return
    ax.set_xlabel("trade # (chronological)")
    ax.set_ylabel("15-min sigma (%)")
    ax.set_title("Volatility check: model sigma (red outline) vs market-implied")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "sigma_check.png"), dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--db")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    rows = load_csv(args.csv) if args.csv else load_db(args.db)
    trades = prep(rows)
    if not trades:
        print("no settled trades found")
        return 1
    os.makedirs(args.out, exist_ok=True)
    plot_equity(trades, args.out)
    plot_calibration(trades, args.out)
    plot_entry_buckets(trades, args.out)
    plot_sigma_check(trades, args.out)
    print(f"wrote 4 charts to {args.out}/ from {len(trades)} trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
