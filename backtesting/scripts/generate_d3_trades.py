"""
Generate per-trade PnL logs for the D3-hybrid mean-reversion signal.

Walks every 15-minute window in the 1-min bar history and simulates a
synthetic ATM trade (fill_price=0.50, fee=0.02) whenever the 3-of-5 vote
fires.  Output matches the format expected by research_cli.py Layers 2-5.

Usage:
    python backtesting/scripts/generate_d3_trades.py
    python backtesting/scripts/generate_d3_trades.py --assets BTC ETH
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from backtesting.data.loaders import load_bars
from strategies.features import MarketFeatures
from strategies.signals.fifteen_min_signal import compute_15m_signal

ASSETS       = ["BTC", "ETH", "SOL", "XRP"]
WINDOW_MIN   = 15          # bars per window
HISTORY_BARS = 60          # bars of context fed to the signal
SECONDS_LEFT = 600.0       # seconds left at entry (10 min into 15m window)
FILL_PRICE   = 0.50        # synthetic ATM market
FEE          = 0.02        # Kalshi fee at 50c: ceil(0.07*0.5*0.5*100)/100


def _series_to_timestamps(ts_series: pd.Series) -> pd.DatetimeIndex:
    """Return a UTC DatetimeIndex regardless of input dtype."""
    return pd.DatetimeIndex(pd.to_datetime(ts_series, utc=True))


def _make_features(bars_slice: pd.DataFrame, asset: str) -> MarketFeatures:
    closes = bars_slice["close"].values.astype("float64")
    dti = _series_to_timestamps(bars_slice["timestamp"])

    log_ret = np.diff(np.log(np.maximum(closes, 1e-8)))
    vol = float(np.std(log_ret)) if len(log_ret) > 5 else 0.01

    prices_60m: deque = deque(maxlen=3600)
    for ts, px in zip(dti, closes):
        prices_60m.append((ts.timestamp(), float(px)))

    current_price = float(closes[-1])
    return MarketFeatures(
        asset=asset,
        ticker=f"{asset}-15m",
        timestamp=dti[-1].timestamp(),
        current_price=current_price,
        strike=current_price,
        btc_price=current_price,
        seconds_left=SECONDS_LEFT,
        elapsed_seconds=300.0,
        yes_ask=50.0,
        no_ask=50.0,
        yes_bid=48.0,
        no_bid=48.0,
        spread_yes=2.0,
        spread_no=2.0,
        realized_vol_1min=vol,
        prices_60m=prices_60m,
    )


def generate_trades(asset: str) -> pd.DataFrame:
    bars = load_bars(asset, check_min_history=False)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    closes = bars["close"].values.astype("float64")
    dti_all = _series_to_timestamps(bars["timestamp"])
    n = len(bars)
    print(f"  {n:,} bars loaded")

    rows = []
    step = 0
    while step + WINDOW_MIN < n:
        ctx_start = max(0, step - HISTORY_BARS)
        ctx = bars.iloc[ctx_start:step]
        if len(ctx) < 32:
            step += WINDOW_MIN
            continue

        features = _make_features(ctx, asset)
        result = compute_15m_signal(features)

        if result is None:
            step += WINDOW_MIN
            continue

        side, _, _ = result
        strike = closes[step]
        outcome_close = closes[step + WINDOW_MIN - 1]
        label = 1 if outcome_close > strike else 0

        won = (label == 1 and side == "yes") or (label == 0 and side == "no")
        pnl = (1.0 - FILL_PRICE - FEE) if won else (0.0 - FILL_PRICE - FEE)

        rows.append({
            "timestamp": dti_all[step],
            "asset": asset,
            "side": side,
            "strike": round(strike, 4),
            "outcome_close": round(outcome_close, 4),
            "label": label,
            "fill_price": FILL_PRICE,
            "fee": FEE,
            "pnl": round(pnl, 4),
            "win": int(won),
        })
        step += WINDOW_MIN

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=ASSETS)
    parser.add_argument("--out-dir", default=str(_ROOT / "backtesting" / "output"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for asset in args.assets:
        asset = asset.upper()
        print(f"\n== {asset} ==")
        df = generate_trades(asset)
        if df.empty:
            print(f"  No trades generated.")
            continue

        fired = len(df)
        wr = df["win"].mean()
        total_pnl = df["pnl"].sum()
        print(f"  {fired:,} trades  |  WR={wr:.1%}  |  total PnL={total_pnl:+.1f}")

        out_path = out_dir / f"{asset.lower()}_trades.csv"
        df.to_csv(out_path, index=False)
        print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
