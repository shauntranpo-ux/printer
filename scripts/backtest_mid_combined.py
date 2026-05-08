"""
Backtest for MidWindowStrategy (ETH+BTC cross=0 at t=15min)
combined with DwellWindowStrategy (ETH, t=30-42min)
combined with LateWindowStrategy (ETH+BTC, t>=45min).

Pass order:
  1. MidWindow   ETH: t=15min, ETH_cross=0 AND BTC_cross=0 (both on same side)
  2. DwellWindow ETH: t=30-42min, dwell>=80%, streak>=60% (skips mid-traded windows)
  3. LateWindow  ETH: t>=45min (skips mid+dwell-traded windows)
  4. LateWindow  BTC: t>=45min (independent)

OOS result for MidWindow alone (TEST 2024-2026):
  N=2,826/119.3wk = 23.7/wk  WR=78.2%  entry=74.9c  EV=+$0.75/trade  $17.8/wk
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
import math
from typing import NamedTuple
import pandas as pd
import numpy as np


# ── Hourly event types ────────────────────────────────────────────────────────

class _OrderBook(NamedTuple):
    yes_ask: float
    no_ask: float


class _HourlyEvent(NamedTuple):
    window_start_ts: float
    elapsed_seconds: float
    eval_ts: float
    strike: float
    current_price: float
    close_price: float
    orderbook: _OrderBook


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _digital_call_price(spot: float, strike: float, t_min: float, sigma_ann: float) -> float:
    """P(S_T > K) under log-normal risk-neutral measure."""
    if t_min <= 0:
        return 1.0 if spot > strike else 0.0
    t = t_min / (365 * 24 * 60)
    s = sigma_ann * math.sqrt(t)
    if s < 1e-10 or strike <= 0 or spot <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = math.log(spot / strike) / s - 0.5 * s
    return float(np.clip(_norm_cdf(d2), 0.0, 1.0))


def generate_hourly_events(df: pd.DataFrame, asset: str, start: float, end: float, seed: int = 42):
    """
    Yield per-minute _HourlyEvent objects for each 60-min window in [start, end].

    Strike = price at window open. Close = price at window close.
    Orderbook prices use Black-Scholes digital call + 2.5c half-spread.
    Realized vol estimated from 60 1m bars before window open.
    """
    SPREAD = 2.5
    ts_arr = df["timestamp"].values.astype(np.int64)
    cl_arr = df["close"].values.astype(np.float64)

    t = int(math.ceil(start / 3600) * 3600)
    while t + 3600 <= end:
        open_idx = int(np.searchsorted(ts_arr, t, side="left"))
        if open_idx >= len(ts_arr):
            t += 3600
            continue
        strike = float(cl_arr[open_idx])

        close_ts = t + 3600 - 60
        close_idx = int(np.searchsorted(ts_arr, close_ts, side="left"))
        close_idx = min(close_idx, len(ts_arr) - 1)
        close_price = float(cl_arr[close_idx])

        # 1h lookback realized vol (annualized)
        v0 = max(0, open_idx - 60)
        hist = cl_arr[v0:open_idx + 1]
        if len(hist) >= 5:
            lr = np.diff(np.log(hist))
            sigma_ann = float(math.sqrt(max(float(np.var(lr)), 1e-12) * 60 * 24 * 365))
        else:
            sigma_ann = 0.60

        for minute in range(1, 61):
            eval_ts = float(t + minute * 60)
            price_idx = int(np.searchsorted(ts_arr, int(eval_ts), side="right")) - 1
            if price_idx < 0 or price_idx >= len(ts_arr):
                continue
            cur = float(cl_arr[price_idx])
            t_rem = float(60 - minute)

            if cur >= strike:
                p_yes = _digital_call_price(cur, strike, t_rem, sigma_ann)
            else:
                p_yes = 1.0 - _digital_call_price(strike, cur, t_rem, sigma_ann)

            yes_ask = float(np.clip(p_yes * 100 + SPREAD, 1.0, 99.0))
            no_ask  = float(np.clip((1.0 - p_yes) * 100 + SPREAD, 1.0, 99.0))

            yield _HourlyEvent(
                window_start_ts=float(t),
                elapsed_seconds=float(minute * 60),
                eval_ts=eval_ts,
                strike=strike,
                current_price=cur,
                close_price=close_price,
                orderbook=_OrderBook(yes_ask=yes_ask, no_ask=no_ask),
            )

        t += 3600

STAKE    = 25.0
FEE_RATE = 0.07

PERIODS = [
    ("TRAIN 2022-2023", "2022-01-01", "2023-12-31"),
    ("TEST  2024-2026", "2024-01-01", "2026-04-15"),
]

# ── MidWindow params ──────────────────────────────────────────────────────────
MID_MIN_ELAPSED  = 550
MID_MAX_ELAPSED  = 650
MID_MIN_DIST_PCT = 0.30   # ETH must be >=0.30% from strike (signal quality, not price)
MID_SKIP_HOURS   = {12, 13}

# ── DwellWindow params ────────────────────────────────────────────────────────
DWELL_MIN_ELAPSED = 30 * 60
DWELL_MAX_ELAPSED = 42 * 60
DWELL_THRESHOLD   = 0.80
STREAK_THRESHOLD  = 0.60
DWELL_SKIP_HOURS  = {12, 13}

# ── LateWindow params ─────────────────────────────────────────────────────────
LATE_MIN_ELAPSED   = 45 * 60
LATE_MIN_DIST_PCT  = 0.3
LATE_MIN_ENTRY     = 85.0


def ts_from(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def load_prices(asset: str) -> pd.DataFrame:
    for name in [f"{asset}_1m_extended.parquet", f"{asset}_1m_2026.parquet"]:
        p = Path(f"data/historical/{name}")
        if p.exists():
            df = pd.read_parquet(p)
            if "open_time" in df.columns and "timestamp" not in df.columns:
                df["timestamp"] = (
                    df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
                )
            return df.sort_values("timestamp").reset_index(drop=True)[["timestamp", "close"]]
    raise FileNotFoundError(asset)


def build_index(df: pd.DataFrame):
    ts_arr = df["timestamp"].values.astype(np.int64)
    cl_arr = df["close"].values.astype(np.float64)
    idx    = np.argsort(ts_arr)
    return ts_arr[idx], cl_arr[idx]


def window_prices(ts_arr, cl_arr, t_start: float, t_end: float):
    lo = int(np.searchsorted(ts_arr, int(t_start), side="left"))
    hi = int(np.searchsorted(ts_arr, int(t_end),   side="right"))
    return cl_arr[lo:hi]


def cross_count(prices, strike: float) -> int:
    if len(prices) < 2:
        return 0
    above = prices > strike
    return int(np.sum(np.diff(above.astype(np.int8)) != 0))


def utc_hour(ts_: float) -> int:
    return datetime.fromtimestamp(ts_, tz=timezone.utc).hour


def dwell_features(prices, strike: float) -> dict | None:
    if len(prices) < 10:
        return None
    above = prices > strike
    n     = len(above)
    current_itm = bool(above[-1])
    dwell_itm   = sum(1 for a in above if a == current_itm) / n
    streak = 0
    for a in reversed(above):
        if a == current_itm:
            streak += 1
        else:
            break
    return {
        "dwell_itm":   dwell_itm,
        "streak_frac": streak / n,
        "is_itm":      current_itm,
    }


def pnl_for(won: bool, entry_c: float) -> float:
    frac      = entry_c / 100.0
    contracts = STAKE / frac
    if won:
        return contracts * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def quarter(ts_: float) -> str:
    dt = datetime.fromtimestamp(ts_, tz=timezone.utc)
    return f"{dt.year}Q{(dt.month-1)//3+1}"


def run_combined(eth_events: list, btc_events: list,
                 eth_ts, eth_cl, btc_ts, btc_cl) -> dict:
    eth_wins: dict = defaultdict(list)
    for ev in eth_events:
        eth_wins[ev.window_start_ts].append(ev)

    btc_wins: dict = defaultdict(list)
    for ev in btc_events:
        btc_wins[ev.window_start_ts].append(ev)

    trades: list[dict] = []
    mid_traded:   set  = set()
    dwell_traded: set  = set()

    # ── Pass 1: MidWindow on ETH (t=15min, ETH+BTC cross=0) ──────────────
    for wts, evs in eth_wins.items():
        if utc_hour(wts) in MID_SKIP_HOURS:
            continue

        # Find the t=15min eval event
        mid_ev = None
        for ev in evs:
            if MID_MIN_ELAPSED <= ev.elapsed_seconds <= MID_MAX_ELAPSED:
                mid_ev = ev
                break
        if mid_ev is None:
            continue

        eval_ts = mid_ev.eval_ts

        # ETH cross_count in [window_start, eval_ts]
        eth_w = window_prices(eth_ts, eth_cl, wts, eval_ts)
        if len(eth_w) < 5:
            continue
        eth_xc = cross_count(eth_w, mid_ev.strike)
        if eth_xc > 0:
            continue
        eth_itm = bool(eth_w[-1] > mid_ev.strike)

        # BTC cross_count in same window
        btc_at_start = window_prices(btc_ts, btc_cl, wts - 60, wts + 120)
        if len(btc_at_start) == 0:
            continue
        btc_strike_approx = float(btc_at_start[0])
        btc_w = window_prices(btc_ts, btc_cl, wts, eval_ts)
        if len(btc_w) < 5:
            continue
        btc_xc = cross_count(btc_w, btc_strike_approx)
        if btc_xc > 0:
            continue
        btc_itm = bool(btc_w[-1] > btc_strike_approx)

        # Both must be on the same side
        if eth_itm != btc_itm:
            continue

        eth_dist_pct = abs(eth_w[-1] - mid_ev.strike) / mid_ev.strike * 100.0
        if eth_dist_pct < MID_MIN_DIST_PCT:
            continue

        side    = "YES" if eth_itm else "NO"
        entry_c = mid_ev.orderbook.yes_ask if eth_itm else mid_ev.orderbook.no_ask
        won_yes = mid_ev.close_price > mid_ev.strike
        won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)

        trades.append({
            "source": "mid_eth",
            "ts":     eval_ts,
            "side":   side,
            "entry":  entry_c,
            "won":    won,
            "pnl":    pnl_for(won, entry_c),
            "q":      quarter(eval_ts),
        })
        mid_traded.add(wts)

    # ── Pass 2: DwellWindow on ETH (skip mid-traded) ──────────────────────
    for wts, evs in eth_wins.items():
        if wts in mid_traded:
            continue
        if utc_hour(wts) in DWELL_SKIP_HOURS:
            continue
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if not (DWELL_MIN_ELAPSED <= ev.elapsed_seconds <= DWELL_MAX_ELAPSED):
                continue
            eth_w = window_prices(eth_ts, eth_cl, wts, ev.eval_ts)
            pf    = dwell_features(eth_w, ev.strike)
            if pf is None:
                continue
            if pf["dwell_itm"] < DWELL_THRESHOLD or pf["streak_frac"] < STREAK_THRESHOLD:
                continue
            side    = "YES" if pf["is_itm"] else "NO"
            entry_c = ev.orderbook.yes_ask if pf["is_itm"] else ev.orderbook.no_ask
            won_yes = ev.close_price > ev.strike
            won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            trades.append({
                "source": "dwell_eth",
                "ts":     ev.eval_ts,
                "side":   side,
                "entry":  entry_c,
                "won":    won,
                "pnl":    pnl_for(won, entry_c),
                "q":      quarter(ev.eval_ts),
            })
            dwell_traded.add(wts)
            break

    # ── Pass 3: LateWindow on ETH (skip mid+dwell-traded) ─────────────────
    for wts, evs in eth_wins.items():
        if wts in mid_traded or wts in dwell_traded:
            continue
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY:
                continue
            won_yes = ev.close_price > ev.strike
            won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            trades.append({
                "source": "late_eth",
                "ts":     ev.eval_ts,
                "side":   side,
                "entry":  entry_c,
                "won":    won,
                "pnl":    pnl_for(won, entry_c),
                "q":      quarter(ev.eval_ts),
            })
            break

    # ── Pass 4: LateWindow on BTC (independent) ───────────────────────────
    for wts, evs in btc_wins.items():
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY:
                continue
            won_yes = ev.close_price > ev.strike
            won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            trades.append({
                "source": "late_btc",
                "ts":     ev.eval_ts,
                "side":   side,
                "entry":  entry_c,
                "won":    won,
                "pnl":    pnl_for(won, entry_c),
                "q":      quarter(ev.eval_ts),
            })
            break

    if not trades:
        return {"n": 0}

    trades.sort(key=lambda t: t["ts"])
    n       = len(trades)
    wins    = sum(1 for t in trades if t["won"])
    total   = sum(t["pnl"] for t in trades)
    days    = (trades[-1]["ts"] - trades[0]["ts"]) / 86400
    ann     = total / days * 365
    weeks   = days / 7

    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak   = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_src: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "esum": 0.0})
    by_q:   dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in trades:
        s = by_src[t["source"]]
        s["n"] += 1; s["w"] += t["won"]; s["pnl"] += t["pnl"]; s["esum"] += t["entry"]
        d = by_q[t["q"]]
        d["n"] += 1; d["w"] += t["won"]; d["pnl"] += t["pnl"]

    return {
        "n": n, "wins": wins, "wr": wins/n*100,
        "pnl": total, "ann": ann, "max_dd": max_dd,
        "days": days, "weeks": weeks,
        "by_src": dict(by_src), "by_q": dict(by_q),
        "n_mid": len(mid_traded), "n_dwell": len(dwell_traded),
    }


def print_results(r: dict, label: str) -> None:
    if r["n"] == 0:
        print("No trades.")
        return
    n, wins, wr = r["n"], r["wins"], r["wr"]
    wk_ev = r["pnl"] / r["weeks"]

    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"{'='*72}")
    print(f"  Trades: {n:,}   WR: {wr:.1f}%   "
          f"PnL: ${r['pnl']:+,.2f}   MaxDD: ${r['max_dd']:,.2f}")
    print(f"  Annual: ${r['ann']:+,.0f}/yr   Weekly EV at $25: ${wk_ev:+.1f}/wk")
    print(f"  Mid windows: {r['n_mid']:,}   Dwell windows: {r['n_dwell']:,}")

    print(f"\n  By source:")
    for src, d in sorted(r["by_src"].items()):
        if d["n"] == 0:
            continue
        src_wr = d["w"] / d["n"] * 100
        avg_e  = d["esum"] / d["n"]
        ev_t   = d["pnl"] / d["n"]
        wk     = d["pnl"] / r["weeks"]
        print(f"    {src:<14}: N={d['n']:>6}  WR={src_wr:>5.1f}%  "
              f"AvgE={avg_e:>5.1f}c  EV=${ev_t:>+6.2f}/t  ${wk:>+6.1f}/wk")

    print(f"\n  Quarterly breakdown:")
    cum = 0.0
    for q in sorted(r["by_q"]):
        d    = r["by_q"][q]
        q_wr = d["w"] / d["n"] * 100 if d["n"] else 0
        cum += d["pnl"]
        print(f"    {q}  n={d['n']:>5}  WR={q_wr:>5.1f}%  "
              f"PnL=${d['pnl']:>+7,.0f}  (cum ${cum:>+8,.0f})")

    print(f"\n  Stake scaling:")
    print(f"    {'Stake':>8}   {'Weekly':>10}   {'Annual':>14}   {'MaxDD':>10}")
    for s in [25, 50, 100, 200, 400, 500, 750, 1000]:
        f  = s / STAKE
        wk = wk_ev * f
        yr = r["ann"] * f
        dd = r["max_dd"] * f
        print(f"    ${s:>7}   ${wk:>+9,.0f}/wk   ${yr:>+13,.0f}/yr   ${dd:>9,.0f}")


def main():
    print("Loading price data...")
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")
    print("Building fast indices...")
    eth_ts, eth_cl = build_index(eth_df)
    btc_ts, btc_cl = build_index(btc_df)

    for label, start_str, end_str in PERIODS:
        start, end = ts_from(start_str), ts_from(end_str)
        print(f"\nGenerating ETH events {label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,}")
        print(f"Generating BTC events {label}...", end=" ", flush=True)
        btc_events = list(generate_hourly_events(btc_df, "BTC", start, end, seed=42))
        print(f"{len(btc_events):,}")

        r = run_combined(eth_events, btc_events, eth_ts, eth_cl, btc_ts, btc_cl)
        print_results(r, label)


if __name__ == "__main__":
    main()
