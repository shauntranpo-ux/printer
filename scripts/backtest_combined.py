"""
Combined strategy backtest: Early-window BTC momentum + Late-window persistence.

Early window (ETH only):
  Signal: |BTC 10-min return| >= 0.5% evaluated at t=10-20min into the hour.
  Action: Take first qualifying eval with taker price (no lag filter — lag signal
  overfit badly, collapsing from 90.5% WR train → 62.4% WR test).
  Expected: WR~73%, entry~69c, EV~$0.87/trade at $25 stake.

Late window (ETH + BTC):
  Signal: |dist from strike| >= 0.3% at t>=45min, entry >= 85c.
  ETH windows already traded by early strategy are skipped (no double-trade).
  Expected: WR~97%, entry~95-96c, EV~$0.29/trade at $25 stake.

Combined OOS (2024-2026): ~$61.8/wk at $25 stake.
Scale to $400/trade for $1k/week target.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
# hourly_window_generator removed (dead code)
import pandas as pd

# ── early-window config ──────────────────────────────────────────────────────
EARLY_MIN_ELAPSED_SEC = 10 * 60   # start looking at t>=10min
EARLY_MAX_ELAPSED_SEC = 20 * 60   # stop looking after t=20min
BTC_SIGNAL_THRESH_PCT = 0.5        # |BTC 10-min return| must be >= this

# ── late-window config ───────────────────────────────────────────────────────
LATE_MIN_ELAPSED_SEC = 45 * 60    # t>=45min
LATE_MIN_DIST_PCT    = 0.3        # |dist from strike| >= 0.3%
LATE_MIN_ENTRY_CENTS = 85.0       # entry >= 85c

# ── shared ────────────────────────────────────────────────────────────────────
STAKE    = 25.0
FEE_RATE = 0.07

ASSETS = ["ETH", "BTC"]

PERIODS = [
    ("TRAIN 2022-2023", "2022-01-01", "2023-12-31"),
    ("TEST  2024-2026", "2024-01-01", "2026-04-15"),
]


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


def btc_10m_return(btc_prices: pd.DataFrame, eval_ts: float) -> float | None:
    """BTC return over the 10 minutes ending at eval_ts."""
    t_start = eval_ts - 10 * 60
    mask = (btc_prices["timestamp"] >= t_start) & (btc_prices["timestamp"] <= eval_ts)
    rows = btc_prices[mask]
    if len(rows) < 5:
        return None
    p_start = rows.iloc[0]["close"]
    p_end   = rows.iloc[-1]["close"]
    return (p_end - p_start) / p_start * 100.0


def pnl_for_trade(won: bool, entry_c: float) -> float:
    entry_frac = entry_c / 100.0
    contracts  = STAKE / entry_frac
    if won:
        gross = contracts * (1.0 - entry_frac)
        return gross * (1.0 - FEE_RATE)
    return -STAKE


def quarter(ts_: float) -> str:
    dt = datetime.fromtimestamp(ts_, tz=timezone.utc)
    return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"


def run_combined(events_eth: list, events_btc: list,
                 btc_prices: pd.DataFrame) -> dict:
    # Group events by window key
    eth_windows: dict = defaultdict(list)
    for ev in events_eth:
        eth_windows[ev.window_start_ts].append(ev)

    btc_windows: dict = defaultdict(list)
    for ev in events_btc:
        btc_windows[ev.window_start_ts].append(ev)

    trades: list[dict] = []
    early_traded_windows: set = set()   # ETH windows traded early → skip late

    # ── Pass 1: early window on ETH ─────────────────────────────────────────
    for wts, evs in eth_windows.items():
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if not (EARLY_MIN_ELAPSED_SEC <= ev.elapsed_seconds <= EARLY_MAX_ELAPSED_SEC):
                continue

            btc_ret = btc_10m_return(btc_prices, ev.eval_ts)
            if btc_ret is None:
                continue
            if abs(btc_ret) < BTC_SIGNAL_THRESH_PCT:
                continue

            # Trade direction: follow BTC
            if btc_ret > 0:
                side, entry_c = "YES", ev.orderbook.yes_ask
            else:
                side, entry_c = "NO", ev.orderbook.no_ask

            won_yes = ev.close_price > ev.strike
            won = (side == "YES" and won_yes) or (side == "NO" and not won_yes)

            trades.append({
                "source": "early",
                "ts": ev.eval_ts,
                "asset": "ETH",
                "side": side,
                "entry_c": entry_c,
                "won": won,
                "pnl": pnl_for_trade(won, entry_c),
                "quarter": quarter(ev.eval_ts),
                "elapsed": ev.elapsed_seconds / 60,
            })
            early_traded_windows.add(wts)
            break  # one trade per window

    # ── Pass 2: late window on ETH (skip windows already traded early) ───────
    for wts, evs in eth_windows.items():
        if wts in early_traded_windows:
            continue
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED_SEC:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100.0
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY_CENTS:
                continue
            won_yes = ev.close_price > ev.strike
            won = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            trades.append({
                "source": "late_eth",
                "ts": ev.eval_ts,
                "asset": "ETH",
                "side": side,
                "entry_c": entry_c,
                "won": won,
                "pnl": pnl_for_trade(won, entry_c),
                "quarter": quarter(ev.eval_ts),
                "elapsed": ev.elapsed_seconds / 60,
            })
            break

    # ── Pass 3: late window on BTC (independent) ────────────────────────────
    for wts, evs in btc_windows.items():
        for ev in sorted(evs, key=lambda e: e.eval_ts):
            if ev.elapsed_seconds < LATE_MIN_ELAPSED_SEC:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100.0
            if pct >= LATE_MIN_DIST_PCT:
                side, entry_c = "YES", ev.orderbook.yes_ask
            elif pct <= -LATE_MIN_DIST_PCT:
                side, entry_c = "NO", ev.orderbook.no_ask
            else:
                continue
            if entry_c < LATE_MIN_ENTRY_CENTS:
                continue
            won_yes = ev.close_price > ev.strike
            won = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            trades.append({
                "source": "late_btc",
                "ts": ev.eval_ts,
                "asset": "BTC",
                "side": side,
                "entry_c": entry_c,
                "won": won,
                "pnl": pnl_for_trade(won, entry_c),
                "quarter": quarter(ev.eval_ts),
                "elapsed": ev.elapsed_seconds / 60,
            })
            break

    trades.sort(key=lambda t: t["ts"])

    n         = len(trades)
    wins      = sum(1 for t in trades if t["won"])
    pnl_total = sum(t["pnl"] for t in trades)

    cumulative = peak = max_dd = 0.0
    for t in trades:
        cumulative += t["pnl"]
        if cumulative > peak:
            peak = cumulative
        max_dd = max(max_dd, peak - cumulative)

    days = (trades[-1]["ts"] - trades[0]["ts"]) / 86400.0 if trades else 1
    ann  = pnl_total / days * 365 if days > 0 else 0

    by_source: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "entry_sum": 0.0})
    by_q:      dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})

    for t in trades:
        s = by_source[t["source"]]
        s["n"] += 1; s["w"] += t["won"]; s["pnl"] += t["pnl"]
        s["entry_sum"] += t["entry_c"]
        d = by_q[t["quarter"]]
        d["n"] += 1; d["w"] += t["won"]; d["pnl"] += t["pnl"]

    return {
        "n": n, "wins": wins, "wr": wins/n*100 if n else 0,
        "pnl": pnl_total, "ann": ann, "max_dd": max_dd,
        "days": days, "by_source": dict(by_source), "by_q": dict(by_q),
        "n_early_skipped_late": len(early_traded_windows),
    }


def print_results(r: dict, label: str) -> None:
    n, wins, wr = r["n"], r["wins"], r["wr"]
    ann, max_dd, days = r["ann"], r["max_dd"], r["days"]
    weeks = days / 7

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Trades: {n:,}   Wins: {wins:,}   WR: {wr:.1f}%   ({days:.0f} days, {weeks:.0f} weeks)")
    print(f"  Total PnL: ${r['pnl']:+,.2f}   Annual: ${ann:+,.0f}/yr")
    print(f"  Max drawdown: ${max_dd:,.2f}")
    print(f"  Early windows where late was skipped: {r['n_early_skipped_late']:,}")

    print(f"\n  By source:")
    for src, d in sorted(r["by_source"].items()):
        if d["n"] == 0:
            continue
        src_wr  = d["w"] / d["n"] * 100
        avg_e   = d["entry_sum"] / d["n"]
        src_ev  = d["pnl"] / d["n"]
        src_wk  = d["pnl"] / weeks
        print(f"    {src:<12}: N={d['n']:>6}  WR={src_wr:>5.1f}%  "
              f"AvgEntry={avg_e:>5.1f}c  EV/trade=${src_ev:>+6.2f}  "
              f"${src_wk:>+6.1f}/wk")

    print(f"\n  Quarterly breakdown:")
    cum = 0.0
    for q in sorted(r["by_q"]):
        d = r["by_q"][q]
        q_wr  = d["w"] / d["n"] * 100 if d["n"] else 0
        cum  += d["pnl"]
        print(f"    {q}  n={d['n']:>5}  WR={q_wr:>5.1f}%  "
              f"PnL=${d['pnl']:>+7,.0f}  (cum ${cum:>+8,.0f})")

    weekly_ev = r["pnl"] / weeks
    print(f"\n  Weekly EV at $25 stake: ${weekly_ev:+.1f}/wk")
    print(f"\n  Stake scaling (to hit target weekly income):")
    print(f"    {'Stake':>8}   {'Weekly':>10}   {'Annual':>14}   {'MaxDD':>10}")
    for s in [25, 50, 100, 200, 400, 500, 750, 1000]:
        f   = s / STAKE
        wk  = weekly_ev * f
        yr  = ann * f
        dd  = max_dd * f
        print(f"    ${s:>7}   ${wk:>+9,.0f}/wk   ${yr:>+13,.0f}/yr   ${dd:>9,.0f}")


def main():
    print("Loading price data...")
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")
    # BTC price map for fast lookup in early window
    btc_prices = btc_df  # full DataFrame, queried by timestamp range

    for label, start_str, end_str in PERIODS:
        start, end = ts(start_str), ts(end_str)
        print(f"\nLoading ETH events for {label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,} events")
        print(f"Loading BTC events for {label}...", end=" ", flush=True)
        btc_events = list(generate_hourly_events(btc_df, "BTC", start, end, seed=42))
        print(f"{len(btc_events):,} events")

        r = run_combined(eth_events, btc_events, btc_prices)
        print_results(r, label)


if __name__ == "__main__":
    main()
