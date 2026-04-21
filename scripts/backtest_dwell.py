"""
Backtest for DwellWindowStrategy (ETH path-features, t=30-42min)
combined with LateWindowStrategy (ETH+BTC, t>=45min).

DwellWindow: enters at t=30-42min when ETH has been on winning side for
>=80% of window AND current streak >= 60% of window. Skips 12-13 UTC.
OOS result: WR=86.9%, entry=83.9c, EV=+$0.63/trade, 43.9 setups/week.

Combined with LateWindow for windows not traded by DwellWindow.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
from strategies.backtest.hourly_window_generator import generate_hourly_events
import pandas as pd
import numpy as np

STAKE    = 25.0
FEE_RATE = 0.07

ASSETS = ["ETH", "BTC"]

PERIODS = [
    ("TRAIN 2022-2023", "2022-01-01", "2023-12-31"),
    ("TEST  2024-2026", "2024-01-01", "2026-04-15"),
]

# ── Dwell strategy params (mirrors DwellWindowStrategy constants) ─────────────
DWELL_MIN_ELAPSED_SEC = 30 * 60
DWELL_MAX_ELAPSED_SEC = 42 * 60
DWELL_THRESHOLD       = 0.80
STREAK_THRESHOLD      = 0.60
SKIP_HOURS_UTC        = {12, 13}

# ── Late strategy params ──────────────────────────────────────────────────────
LATE_MIN_ELAPSED_SEC = 45 * 60
LATE_MIN_DIST_PCT    = 0.3
LATE_MIN_ENTRY_CENTS = 85.0


def ts(s: str) -> float:
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


def utc_hour(ts_: float) -> int:
    return datetime.fromtimestamp(ts_, tz=timezone.utc).hour


def dwell_features(eth_df: pd.DataFrame, window_start: float,
                   eval_ts: float, strike: float) -> dict | None:
    mask  = (eth_df["timestamp"] >= window_start) & (eth_df["timestamp"] <= eval_ts)
    prices = eth_df[mask]["close"].values
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
    streak_frac = streak / n
    cross_count = int(np.sum(np.diff(above.astype(int)) != 0))
    return {
        "dwell_itm":   dwell_itm,
        "streak_frac": streak_frac,
        "cross_count": cross_count,
        "is_itm":      current_itm,
    }


def pnl_for(won: bool, entry_c: float) -> float:
    frac      = entry_c / 100
    contracts = STAKE / frac
    if won:
        return contracts * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def quarter(ts_: float) -> str:
    dt = datetime.fromtimestamp(ts_, tz=timezone.utc)
    return f"{dt.year}Q{(dt.month-1)//3+1}"


def run_combined(events_eth: list, events_btc: list, eth_df: pd.DataFrame) -> dict:
    eth_wins: dict  = defaultdict(list)
    for ev in events_eth:
        eth_wins[ev.window_start_ts].append(ev)

    btc_wins: dict  = defaultdict(list)
    for ev in events_btc:
        btc_wins[ev.window_start_ts].append(ev)

    trades: list[dict] = []
    dwell_traded: set  = set()

    # ── Pass 1: DwellWindow on ETH ──────────────────────────────────────────
    for wts, evs in eth_wins.items():
        if utc_hour(wts) in SKIP_HOURS_UTC:
            continue
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if not (DWELL_MIN_ELAPSED_SEC <= ev.elapsed_seconds <= DWELL_MAX_ELAPSED_SEC):
                continue
            pf = dwell_features(eth_df, wts, ev.eval_ts, ev.strike)
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

    # ── Pass 2: LateWindow on ETH (skip dwell-traded) ───────────────────────
    for wts, evs in eth_wins.items():
        if wts in dwell_traded:
            continue
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED_SEC:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY_CENTS:
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

    # ── Pass 3: LateWindow on BTC (independent) ─────────────────────────────
    for wts, evs in btc_wins.items():
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED_SEC:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY_CENTS:
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

    trades.sort(key=lambda t: t["ts"])
    n       = len(trades)
    wins    = sum(1 for t in trades if t["won"])
    total   = sum(t["pnl"] for t in trades)
    days    = (trades[-1]["ts"] - trades[0]["ts"]) / 86400 if trades else 1
    ann     = total / days * 365

    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_src: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "esum": 0.0})
    by_q:   dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in trades:
        s = by_src[t["source"]]
        s["n"] += 1; s["w"] += t["won"]; s["pnl"] += t["pnl"]; s["esum"] += t["entry"]
        d = by_q[t["q"]]
        d["n"] += 1; d["w"] += t["won"]; d["pnl"] += t["pnl"]

    return {
        "n": n, "wins": wins, "wr": wins/n*100 if n else 0,
        "pnl": total, "ann": ann, "max_dd": max_dd, "days": days,
        "by_src": dict(by_src), "by_q": dict(by_q),
        "n_dwell": len(dwell_traded),
    }


def print_results(r: dict, label: str) -> None:
    n, wins, wr = r["n"], r["wins"], r["wr"]
    weeks = r["days"] / 7
    wk_ev = r["pnl"] / weeks

    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"{'='*72}")
    print(f"  Trades: {n:,}   WR: {wr:.1f}%   "
          f"PnL: ${r['pnl']:+,.2f}   MaxDD: ${r['max_dd']:,.2f}")
    print(f"  Annual: ${r['ann']:+,.0f}/yr   Weekly EV at $25: ${wk_ev:+.1f}/wk")
    print(f"  Dwell windows traded: {r['n_dwell']:,}")

    print(f"\n  By source:")
    for src, d in sorted(r["by_src"].items()):
        if d["n"] == 0:
            continue
        src_wr  = d["w"] / d["n"] * 100
        avg_e   = d["esum"] / d["n"]
        ev_t    = d["pnl"] / d["n"]
        wk      = d["pnl"] / weeks
        print(f"    {src:<14}: N={d['n']:>6}  WR={src_wr:>5.1f}%  "
              f"AvgEntry={avg_e:>5.1f}c  EV=${ev_t:>+6.2f}/trade  ${wk:>+6.1f}/wk")

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

    for label, start_str, end_str in PERIODS:
        start, end = ts(start_str), ts(end_str)
        print(f"\nLoading ETH events for {label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,}")
        print(f"Loading BTC events for {label}...", end=" ", flush=True)
        btc_events = list(generate_hourly_events(btc_df, "BTC", start, end, seed=42))
        print(f"{len(btc_events):,}")

        r = run_combined(eth_events, btc_events, eth_df)
        print_results(r, label)


if __name__ == "__main__":
    main()
