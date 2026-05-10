#!/usr/bin/env python3
"""
scripts/backtest_s1s2.py — S1/S2 strategy backtest: v1 vs v2 thresholds.

Runs against Binance 1-min OHLCV data (data/{ASSET}_1m.csv).
For each 15-min window, tries entry at each valid minute; takes first gate-pass.
OBI gate skipped for S2 (no historical orderbook snapshots available).
P&L uses actual outcomes (final_close vs strike), not win-rate lookup tables.
EV gate uses tanh proxy to avoid lookahead bias from calibration tables.

Usage:
    py scripts/backtest_s1s2.py
    py scripts/backtest_s1s2.py --start-year 2024
    py scripts/backtest_s1s2.py --start-year 2023 --end-year 2024
"""
import argparse
import math
import os
import random
import sys
from collections import deque
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest as bt
from bot_strategy import _S1_ASSET_CONFIG as _LIVE_S1, _S2_ASSET_CONFIG as _LIVE_S2

# ---------------------------------------------------------------------------
# S1 configs — v1 (original) and v2 (loosened x0.5)
# ---------------------------------------------------------------------------

S1_V1: dict = {
    "BTC":  dict(min_dist=0.0025, max_rv=0.0080, ema_short=3, ema_long=10,
                 session_gate=True,  min_ev=0.08, time_min=3.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=0.0120, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.09, time_min=3.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=0.0200, ema_short=3, ema_long=8,
                 session_gate=False, min_ev=0.10, time_min=3.0, time_max=10.0),
    "XRP":  dict(min_dist=0.0040, max_rv=0.0160, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.09, time_min=3.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0080, max_rv=0.0300, ema_short=2, ema_long=8,
                 session_gate=False, min_ev=0.12, time_min=3.0, time_max=10.0),
}

S1_V2: dict = {
    "BTC":  dict(min_dist=0.00125, max_rv=0.0160, ema_short=3, ema_long=10,
                 session_gate=True,  min_ev=0.04, time_min=3.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0015,  max_rv=0.0240, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.045, time_min=3.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0025,  max_rv=0.0400, ema_short=3, ema_long=8,
                 session_gate=False, min_ev=0.05, time_min=3.0, time_max=10.0),
    "XRP":  dict(min_dist=0.0020,  max_rv=0.0320, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.045, time_min=3.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0040,  max_rv=0.0600, ema_short=2, ema_long=8,
                 session_gate=False, min_ev=0.06, time_min=3.0, time_max=10.0),
}

# ---------------------------------------------------------------------------
# S2 configs — v1 (original) and v2 (loosened x0.5)
# ---------------------------------------------------------------------------

S2_V1: dict = {
    # min_vel_delta in cents, calibrated for theoretical YES-ask model (~6x live market values)
    "BTC":  dict(min_dist=0.0035, min_vel_delta=5.0, vel_lookback=4,
                 min_ev=0.09, time_min=2.0, time_max=13.0),
    "ETH":  dict(min_dist=0.0030, min_vel_delta=4.5, vel_lookback=4,
                 min_ev=0.09, time_min=2.0, time_max=13.0),
    "SOL":  dict(min_dist=0.0060, min_vel_delta=7.0, vel_lookback=3,
                 min_ev=0.11, time_min=2.0, time_max=11.0),
    "XRP":  dict(min_dist=0.0050, min_vel_delta=5.5, vel_lookback=4,
                 min_ev=0.10, time_min=2.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0100, min_vel_delta=9.0, vel_lookback=3,
                 min_ev=0.13, time_min=2.0, time_max=10.0),
}

S2_V2: dict = {
    # min_vel_delta in cents, calibrated for theoretical YES-ask model (~6x live market values)
    "BTC":  dict(min_dist=0.00175, min_vel_delta=2.5, vel_lookback=4,
                 min_ev=0.045, time_min=2.0, time_max=13.0),
    "ETH":  dict(min_dist=0.0015,  min_vel_delta=2.0, vel_lookback=4,
                 min_ev=0.045, time_min=2.0, time_max=13.0),
    "SOL":  dict(min_dist=0.0030,  min_vel_delta=3.5, vel_lookback=3,
                 min_ev=0.055, time_min=2.0, time_max=11.0),
    "XRP":  dict(min_dist=0.0025,  min_vel_delta=2.5, vel_lookback=4,
                 min_ev=0.05,  time_min=2.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0050,  min_vel_delta=4.5, vel_lookback=3,
                 min_ev=0.065, time_min=2.0, time_max=10.0),
}

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
KALSHI_FEE_CENTS = 7
TRADE_AMOUNT = 25.0

# ---------------------------------------------------------------------------
# Signal utilities — operate on small per-window price lists (fast)
# ---------------------------------------------------------------------------

def _ema(vals: list) -> float:
    alpha = 2.0 / (len(vals) + 1)
    v = float(vals[0])
    for x in vals[1:]:
        v = alpha * float(x) + (1.0 - alpha) * v
    return v


def _realized_vol(prices: list) -> float:
    """Std of log-returns. prices = list of floats (already windowed)."""
    if len(prices) < 2:
        return 0.001
    rets = [math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices)) if prices[i - 1] > 0]
    if not rets:
        return 0.001
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) if var > 0 else 0.001


def _build_lookback(prev_minutes: deque, cur_dict: dict, up_to_minute: int,
                    need_minutes: int) -> list:
    """Return the last `need_minutes` of prices ending at `up_to_minute` in cur_dict.

    Pulls from prev_minutes (previous window closes) then current window.
    """
    cur_prices = [cur_dict[m] for m in range(0, up_to_minute + 1) if m in cur_dict]
    combined = list(prev_minutes) + cur_prices
    return combined[-need_minutes:]


def _is_us_session(window_ts: int) -> bool:
    try:
        dt = datetime.fromtimestamp(window_ts, tz=timezone.utc)
        et = dt.astimezone(timezone(timedelta(hours=-4)))
        t = et.hour * 60 + et.minute
        return (9 * 60 + 30 <= t <= 11 * 60 + 30) or (15 * 60 <= t <= 16 * 60)
    except Exception:
        return True


def _ev_gate(win_prob: float, entry_cents: float, min_ev: float) -> bool:
    fee = (KALSHI_FEE_CENTS / 100) * (entry_cents / 100) * (1 - entry_cents / 100)
    return (win_prob - entry_cents / 100 - fee) >= min_ev


def _s1_win_prob(abs_pct: float, min_dist: float) -> float:
    return 0.50 + 0.28 * math.tanh(abs_pct / max(min_dist, 1e-6))


def _s2_win_prob(vel_delta: float, min_vel: float) -> float:
    return 0.50 + 0.20 * math.tanh(vel_delta / max(min_vel, 1e-6))


def _theoretical_yes_ask(spot: float, strike: float) -> float:
    """Deterministic YES-ask proxy (cents) calibrated for 15-min windows.
    k=0.012 so 0.5% above strike ≈ 68c, 1.0% ≈ 81c — matches typical Kalshi market pricing.
    (k=0.004 was too steep: 0.28% above already hit 76c ceiling, killing all S2 entries.)
    """
    if strike <= 0:
        return 50.0
    pct = (spot - strike) / strike
    return max(5.0, min(95.0, 50.0 + 45.0 * math.tanh(pct / 0.012)))


def _pnl(won: bool, entry_cents: float) -> float:
    contracts = max(1, int(TRADE_AMOUNT * 100 / entry_cents))
    fee_total = contracts * KALSHI_FEE_CENTS / 100.0
    exit_price = 100.0 if won else 0.0
    return (exit_price - entry_cents) * contracts / 100.0 - fee_total


def _sharpe(pnl_list: list) -> float:
    if len(pnl_list) < 2:
        return 0.0
    mean = sum(pnl_list) / len(pnl_list)
    var = sum((x - mean) ** 2 for x in pnl_list) / len(pnl_list)
    std = math.sqrt(var)
    return mean / std if std > 0 else 0.0


# ---------------------------------------------------------------------------
# S1 backtest
# ---------------------------------------------------------------------------

def backtest_s1_asset(asset: str, cfg: dict, windows, price_lookup,
                      rng: random.Random) -> dict:
    trades, wins, pnl_list = 0, 0, []
    t_min, t_max = cfg["time_min"], cfg["time_max"]
    ema_long = cfg["ema_long"]
    ema_short = cfg["ema_short"]
    rv_window = 5  # minutes for realized vol

    # Rolling buffer: last 20 closes from previous window (for EMA lookback across window boundary)
    prev_closes: deque = deque(maxlen=20)

    for w_ts, row in windows.iterrows():
        strike      = row["strike"]
        final_close = row["final_close"]
        minute_dict = price_lookup.get(w_ts)
        if minute_dict is None or not minute_dict:
            prev_closes.clear()
            continue

        # Session gate — BTC only — blocks whole window
        if cfg.get("session_gate") and not _is_us_session(int(w_ts)):
            # Carry forward this window's prices for next window's lookback
            for m in sorted(minute_dict):
                prev_closes.append(minute_dict[m])
            continue

        traded = False
        for entry_min in range(3, 13):
            mins_left = 15.0 - entry_min
            if not (t_min <= mins_left <= t_max):
                continue
            current_price = minute_dict.get(entry_min) or minute_dict.get(entry_min - 1)
            if not current_price:
                continue

            abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0
            if abs_pct < cfg["min_dist"]:
                continue

            # Realized vol from last rv_window minutes
            rv_prices = _build_lookback(prev_closes, minute_dict, entry_min, rv_window)
            rv = _realized_vol(rv_prices)
            if rv > cfg["max_rv"]:
                continue

            # EMA direction
            short_prices = _build_lookback(prev_closes, minute_dict, entry_min, ema_short)
            long_prices  = _build_lookback(prev_closes, minute_dict, entry_min, ema_long)
            if len(short_prices) < 2 or len(long_prices) < 3:
                continue
            s_ema = _ema(short_prices)
            l_ema = _ema(long_prices)
            direction = "yes" if s_ema > l_ema else "no"

            # Reversal gate
            if direction == "yes" and current_price < strike:
                continue
            if direction == "no" and current_price > strike:
                continue

            side = direction
            yes_ask = _theoretical_yes_ask(current_price, strike)
            no_ask  = 100.0 - yes_ask
            entry_cents = yes_ask if side == "yes" else no_ask
            if not (20 <= entry_cents <= 76):
                continue

            win_prob = _s1_win_prob(abs_pct, cfg["min_dist"])
            if not _ev_gate(win_prob, entry_cents, cfg["min_ev"]):
                continue

            # Trade fires
            settled_yes = final_close > strike
            won = (side == "yes" and settled_yes) or (side == "no" and not settled_yes)
            trades += 1
            if won:
                wins += 1
            pnl_list.append(_pnl(won, entry_cents))
            traded = True
            break

        # Update lookback buffer with this window's prices
        for m in sorted(minute_dict):
            prev_closes.append(minute_dict[m])

    wr = wins / trades if trades else 0.0
    tot = sum(pnl_list)
    return dict(trades=trades, win_rate=wr, total_pnl=tot,
                avg_pnl=tot / trades if trades else 0.0, sharpe=_sharpe(pnl_list))


# ---------------------------------------------------------------------------
# S2 backtest
# ---------------------------------------------------------------------------

def backtest_s2_asset(asset: str, cfg: dict, windows, price_lookup,
                      rng: random.Random) -> dict:
    trades, wins, pnl_list = 0, 0, []
    t_min, t_max = cfg["time_min"], cfg["time_max"]
    vel_lookback = cfg["vel_lookback"]

    prev_closes: deque = deque(maxlen=20)

    for w_ts, row in windows.iterrows():
        strike      = row["strike"]
        final_close = row["final_close"]
        minute_dict = price_lookup.get(w_ts)
        if minute_dict is None or not minute_dict:
            prev_closes.clear()
            continue

        for entry_min in range(2, 13):
            mins_left = 15.0 - entry_min
            if not (t_min <= mins_left <= t_max):
                continue
            current_price = minute_dict.get(entry_min) or minute_dict.get(entry_min - 1)
            if not current_price:
                continue

            abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0
            if abs_pct < cfg["min_dist"]:
                continue

            # Velocity: simulate YES-ask deltas over lookback minutes
            lookback_prices = _build_lookback(prev_closes, minute_dict, entry_min, vel_lookback + 1)
            if len(lookback_prices) < 2:
                continue

            # Velocity: theoretical YES-ask half-half delta (matches live _s2_contract_direction)
            yes_asks   = [_theoretical_yes_ask(p, strike) for p in lookback_prices]
            mid        = max(1, len(yes_asks) // 2)
            first_avg  = sum(yes_asks[:mid]) / mid
            second_avg = sum(yes_asks[mid:]) / max(1, len(yes_asks) - mid)
            vel_delta  = second_avg - first_avg
            if abs(vel_delta) < cfg["min_vel_delta"]:
                continue

            side = "yes" if vel_delta > 0 else "no"
            if side == "yes" and current_price < strike:
                continue
            if side == "no" and current_price > strike:
                continue

            # OBI gate SKIPPED — no historical orderbook data

            yes_ask = _theoretical_yes_ask(current_price, strike)
            no_ask  = 100.0 - yes_ask
            entry_cents = yes_ask if side == "yes" else no_ask
            if not (20 <= entry_cents <= 76):
                continue

            win_prob = _s2_win_prob(abs(vel_delta), cfg["min_vel_delta"])
            if not _ev_gate(win_prob, entry_cents, cfg["min_ev"]):
                continue

            settled_yes = final_close > strike
            won = (side == "yes" and settled_yes) or (side == "no" and not settled_yes)
            trades += 1
            if won:
                wins += 1
            pnl_list.append(_pnl(won, entry_cents))
            break

        for m in sorted(minute_dict):
            prev_closes.append(minute_dict[m])

    wr = wins / trades if trades else 0.0
    tot = sum(pnl_list)
    return dict(trades=trades, win_rate=wr, total_pnl=tot,
                avg_pnl=tot / trades if trades else 0.0, sharpe=_sharpe(pnl_list))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fmt(r: dict) -> str:
    return (f"trades={r['trades']:>6}  wr={r['win_rate']:.1%}  "
            f"pnl=${r['total_pnl']:>+9.2f}  avg=${r['avg_pnl']:>+6.2f}  "
            f"sharpe={r['sharpe']:>+5.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year",   type=int, default=9999)
    parser.add_argument("--amount",     type=float, default=25.0)
    parser.add_argument("--seed",       type=int,   default=42)
    args = parser.parse_args()

    global TRADE_AMOUNT
    TRADE_AMOUNT = args.amount
    rng = random.Random(args.seed)

    yr_label = f"{args.start_year}-{args.end_year}" if args.end_year < 9999 else f"{args.start_year}+"
    print(f"\n{'='*72}")
    print(f"  S1/S2 Backtest | {yr_label} | ${args.amount}/trade | OBI gate skipped for S2")
    print(f"{'='*72}\n")

    for asset in ASSETS:
        print(f"Loading {asset}...", flush=True)
        try:
            windows, price_lookup = bt.load_data(
                asset=asset, start_year=args.start_year, end_year=args.end_year,
                mode="full", verbose=False,
            )
        except FileNotFoundError as exc:
            print(f"  SKIP -- {exc}\n")
            continue

        print(f"\n{'-'*72}")
        print(f"  {asset}  ({len(windows):,} windows)")
        print(f"{'-'*72}")

        for label, cfg in [("S1 original", S1_V1[asset]), ("S1 live", _LIVE_S1[asset])]:
            r = backtest_s1_asset(asset, cfg, windows, price_lookup, rng)
            print(f"  {label:12}  {_fmt(r)}")

        # Live S2 min_vel_delta is calibrated for actual Kalshi market prices (cent ticks).
        # Theoretical YES-ask model produces ~6x larger deltas, so scale threshold up.
        _bt_vel = {"BTC": 2.5, "ETH": 2.0, "SOL": 2.0, "XRP": 2.5, "DOGE": 1.5}
        _live_s2_bt = {**_LIVE_S2[asset], "min_vel_delta": _bt_vel[asset]}

        for label, cfg in [("S2 original", S2_V1[asset]), ("S2 live", _live_s2_bt)]:
            r = backtest_s2_asset(asset, cfg, windows, price_lookup, rng)
            print(f"  {label:12}  {_fmt(r)}")

        print()

    print(f"{'='*72}")
    print("  P&L = actual outcomes (final_close vs strike). EV/win_prob via tanh proxy.")
    print("  S2 uses deterministic theoretical YES-ask (no random AMM). OBI gate skipped.")
    print("  S2 live min_vel_delta scaled x6 for theoretical pricing model.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
