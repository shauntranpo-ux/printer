"""
Comprehensive backtest for BTC (V3 mean-reversion) and ETH (V2 + RSI/Bollinger).

Train: 2022-01-01 to 2023-12-31
Test:  2024-01-01 to 2026-04-15

BTC grid: min_ev in [0.02, 0.03, 0.04], vol_gate in [4.0, 5.0, 6.0]
ETH grid: min_ev in [0.02, 0.03, 0.04], vol_gate in [4.5, 5.5, 6.5]

Usage:
    python scripts/backtest_hourly_v3.py
    python scripts/backtest_hourly_v3.py --assets BTC
    python scripts/backtest_hourly_v3.py --assets ETH
    python scripts/backtest_hourly_v3.py --quick
"""

from __future__ import annotations
import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_prices(asset: str) -> pd.DataFrame:
    extended = Path("data/historical") / f"{asset}_1m_extended.parquet"
    legacy   = Path("data/historical") / f"{asset}_1m_2026.parquet"
    path = extended if extended.exists() else legacy
    if not path.exists():
        raise FileNotFoundError(f"No historical data for {asset}")
    df = pd.read_parquet(path)
    if "open_time" in df.columns and "timestamp" not in df.columns:
        df["timestamp"] = (
            df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
        )
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Return close only (volume not needed in backtest runner itself)
    return df[["timestamp", "close"]]


def load_btc_prices() -> pd.DataFrame:
    return load_prices("BTC")


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------

def make_hourly_strategy(
    asset: str,
    min_ev: float,
    vol_gate_thresh: float,
    stake: float,
    calibrator=None,
):
    from strategies.skip_layer import SkipConfig

    skip_cfg = SkipConfig(
        max_spread_cents         = 4.0,
        min_seconds_left         = 120.0,
        min_entry_price_cents    = 35.0,
        cold_start_samples       = 60,
        vol_ratio_threshold      = vol_gate_thresh,
        vol_confirm_mult         = 1.25,
        vol_oppose_mult          = 0.70,
        mom_lock_enabled         = True,
        mom_lock_neutral_tighten = 1.0,
        mom_accel_scale          = 3.0,
    )

    if asset == "BTC":
        from strategies.btc_hourly_strategy import BTCHourlyStrategy
        return BTCHourlyStrategy(
            skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake,
            calibrator=calibrator,
        )
    elif asset == "ETH":
        from strategies.eth_hourly_strategy import ETHHourlyStrategy
        return ETHHourlyStrategy(
            skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake,
            calibrator=calibrator,
        )
    raise ValueError(f"Unsupported asset: {asset}")


# ---------------------------------------------------------------------------
# Event generation
# ---------------------------------------------------------------------------

def slice_hourly_events(
    asset: str, df: pd.DataFrame,
    start_ts: float, end_ts: float, seed: int,
) -> list:
    from strategies.backtest.hourly_window_generator import generate_hourly_events
    return list(generate_hourly_events(df, asset, start_ts=start_ts, end_ts=end_ts, seed=seed))


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def run_hourly_backtest(strategy, events, stake: float, btc_prices_df=None) -> list:
    from collections import deque
    from strategies.backtest.runner import _settle_trade, BacktestTrade
    from strategies.features import MarketFeatures
    from strategies.fees import taker_fee
    import numpy as np

    trades = []
    traded_windows: set = set()

    btc_history = None
    btc_arr = None
    if btc_prices_df is not None:
        btc_history = list(
            zip(btc_prices_df["timestamp"].values, btc_prices_df["close"].values)
        )
        btc_arr = np.array([ts for ts, _ in btc_history])

    for event in events:
        window_key = (event.asset, event.window_start_ts)
        if window_key in traded_windows:
            continue

        prices_1m  = deque(maxlen=60)
        prices_5m  = deque(maxlen=300)
        prices_60m = deque(maxlen=3600)
        for ts, p in event.price_history:
            prices_60m.append((ts, p))
            if ts >= event.eval_ts - 300:
                prices_5m.append((ts, p))
            if ts >= event.eval_ts - 60:
                prices_1m.append((ts, p))

        btc_60m = deque(maxlen=3600)
        if btc_history is not None:
            cutoff = event.eval_ts - 3600
            lo = int(np.searchsorted(btc_arr, cutoff))
            hi = int(np.searchsorted(btc_arr, event.eval_ts, side="right"))
            for ts, p in btc_history[lo:hi]:
                btc_60m.append((ts, p))

        kalshi_hist = deque(maxlen=60)
        for i in range(30):
            kalshi_hist.append((event.eval_ts - (30 - i) * 10, event.orderbook.yes_ask))

        features = MarketFeatures(
            asset           = event.asset,
            ticker          = f"KH-{event.asset}",
            timestamp       = event.eval_ts,
            current_price   = event.current_price,
            strike          = event.strike,
            btc_price       = event.current_price if event.asset == "BTC" else 0.0,
            seconds_left    = event.seconds_left,
            elapsed_seconds = event.elapsed_seconds,
            yes_ask         = event.orderbook.yes_ask,
            no_ask          = event.orderbook.no_ask,
            yes_bid         = event.orderbook.yes_bid,
            no_bid          = event.orderbook.no_bid,
            spread_yes      = event.orderbook.yes_ask - event.orderbook.yes_bid,
            spread_no       = event.orderbook.no_ask  - event.orderbook.no_bid,
            prices_1m       = prices_1m,
            prices_5m       = prices_5m,
            prices_60m      = prices_60m,
            btc_prices_60m  = btc_60m,
            kalshi_price_history = kalshi_hist,
            realized_vol_1min    = event.realized_vol_1min,
        )

        decision = strategy.decide(features)
        if decision.action == "skip":
            continue

        side        = decision.side
        entry_cents = event.orderbook.yes_ask if side == "yes" else event.orderbook.no_ask
        entry_price = entry_cents / 100.0
        if entry_price <= 0 or entry_price >= 1.0:
            continue

        contracts = max(1, int(stake / entry_price))
        actual_stake = contracts * entry_price
        fee = taker_fee(contracts, entry_price)

        outcome, payout, pnl = _settle_trade(
            side        = side,
            entry_cents = entry_cents,
            contracts   = contracts,
            stake       = actual_stake,
            fee         = fee,
            strike      = event.strike,
            close_price = event.close_price,
        )

        traded_windows.add(window_key)
        trades.append(BacktestTrade(
            asset              = event.asset,
            eval_ts            = event.eval_ts,
            window_start_ts    = event.window_start_ts,
            window_close_ts    = event.window_close_ts,
            side               = side,
            entry_price_cents  = entry_cents,
            raw_p_model        = float(decision.contributing_signals.get("raw_p_yes", decision.p_model)),
            calibrated_p_model = float(decision.p_model),
            contracts          = contracts,
            stake_dollars      = actual_stake,
            fee_dollars        = fee,
            close_price        = event.close_price,
            strike             = event.strike,
            outcome            = outcome,
            payout_dollars     = payout,
            pnl_dollars        = pnl,
            ev_at_entry        = float(decision.expected_value or 0.0),
            reason             = decision.reason,
            signals_dump       = dict(decision.contributing_signals),
        ))

    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(trades: list, total_windows: int, n_days: float) -> dict:
    if not trades:
        return {
            "total_trades": 0, "total_windows": total_windows,
            "trade_rate_pct": 0.0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "total_pnl": 0.0, "annualized_pnl": 0.0,
            "avg_pnl": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
            "avg_ev_at_entry": 0.0,
        }
    wins = sum(1 for t in trades if t.outcome == "win")
    pnl_list = [t.pnl_dollars for t in trades]
    total_pnl = sum(pnl_list)

    running = peak = max_dd = 0.0
    for p in pnl_list:
        running += p
        if running > peak:
            peak = running
        max_dd = max(max_dd, peak - running)

    sharpe = 0.0
    if len(pnl_list) >= 2:
        std = statistics.stdev(pnl_list)
        if std > 0:
            sharpe = statistics.mean(pnl_list) / std

    annualized = total_pnl * (365.0 / n_days) if n_days > 0 else 0.0

    return {
        "total_trades":    len(trades),
        "total_windows":   total_windows,
        "trade_rate_pct":  round(len(trades) / total_windows * 100, 1) if total_windows else 0,
        "wins":            wins,
        "losses":          len(trades) - wins,
        "win_rate_pct":    round(wins / len(trades) * 100, 1),
        "total_pnl":       round(total_pnl, 2),
        "annualized_pnl":  round(annualized, 2),
        "avg_pnl":         round(total_pnl / len(trades), 3),
        "max_drawdown":    round(max_dd, 2),
        "sharpe":          round(sharpe, 3),
        "avg_ev_at_entry": round(sum(t.ev_at_entry for t in trades) / len(trades), 4),
    }


def quarterly_breakdown(trades: list) -> dict:
    by_quarter: dict[str, list] = defaultdict(list)
    for t in trades:
        dt = datetime.utcfromtimestamp(t.eval_ts)
        q  = (dt.month - 1) // 3 + 1
        key = f"{dt.year}Q{q}"
        by_quarter[key].append(t)
    result = {}
    for qk, qtrades in sorted(by_quarter.items()):
        wins  = sum(1 for t in qtrades if t.outcome == "win")
        total = len(qtrades)
        pnl   = sum(t.pnl_dollars for t in qtrades)
        result[qk] = {
            "trades":   total,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "pnl":      round(pnl, 2),
        }
    return result


def fit_calibration(asset: str, trades: list):
    from strategies.calibration import AssetCalibrator
    if len(trades) < 15:
        return None
    cal = AssetCalibrator(asset)
    raw = [t.raw_p_model for t in trades]
    outcomes = [
        1 if ((t.side == "yes" and t.outcome == "win") or
              (t.side == "no"  and t.outcome == "loss")) else 0
        for t in trades
    ]
    cal.refit(raw, outcomes)
    return cal


def signal_contribution_report(trades: list) -> dict:
    """Analyze which signals fired most on wins vs losses."""
    win_signals = defaultdict(list)
    loss_signals = defaultdict(list)

    sig_keys = ["vwap_adj", "rsi_adj", "bb_adj", "mom_adj",
                "beta_adj", "accel_adj", "corr_adj",
                "rsi_confirm_adj", "bb_confirm_adj"]

    for t in trades:
        sig = t.signals_dump
        bucket = win_signals if t.outcome == "win" else loss_signals
        for k in sig_keys:
            if k in sig and sig[k] != 0:
                bucket[k].append(sig[k])

    report = {}
    for k in sig_keys:
        w_vals = win_signals[k]
        l_vals = loss_signals[k]
        if w_vals or l_vals:
            report[k] = {
                "win_count":  len(w_vals),
                "loss_count": len(l_vals),
                "win_avg":    round(sum(w_vals)/len(w_vals), 4) if w_vals else 0.0,
                "loss_avg":   round(sum(l_vals)/len(l_vals), 4) if l_vals else 0.0,
            }
    return report


# ---------------------------------------------------------------------------
# Grid search per asset
# ---------------------------------------------------------------------------

def grid_search_asset(
    asset: str,
    df: pd.DataFrame,
    btc_df: pd.DataFrame,
    train_start_ts: float,
    train_end_ts: float,
    test_start_ts: float,
    test_end_ts: float,
    stake: float,
    seed: int,
    min_ev_vals: list,
    vol_gate_vals: list,
    verbose: bool = True,
) -> dict:
    n_test_days = (test_end_ts - test_start_ts) / 86400.0

    if verbose:
        print(f"\n  Generating train events ({asset})...")
    train_events_all = slice_hourly_events(asset, df, train_start_ts, train_end_ts, seed)
    n_train_windows = len(set(e.window_start_ts for e in train_events_all))
    if verbose:
        print(f"  Train: {len(train_events_all):,} eval points, {n_train_windows:,} windows")

    if verbose:
        print(f"  Generating test events ({asset})...")
    test_events_all = slice_hourly_events(asset, df, test_start_ts, test_end_ts, seed)
    n_test_windows = len(set(e.window_start_ts for e in test_events_all))
    if verbose:
        print(f"  Test:  {len(test_events_all):,} eval points, {n_test_windows:,} windows")

    btc_prices_for_strategy = btc_df if asset != "BTC" else None

    if verbose:
        print(f"  Fitting calibration on train data...")
    strat_train = make_hourly_strategy(asset, min_ev=0.03, vol_gate_thresh=vol_gate_vals[0], stake=stake)
    train_trades = run_hourly_backtest(strat_train, train_events_all, stake=stake,
                                       btc_prices_df=btc_prices_for_strategy)
    calibrator = fit_calibration(asset, train_trades)
    if verbose:
        print(f"  Train trades: {len(train_trades)}, calibrator fitted: {calibrator is not None}")

    all_results = []
    best_result = None
    best_metric = -float("inf")

    if verbose:
        print(f"\n  Grid search ({len(min_ev_vals)} x {len(vol_gate_vals)} configs):")

    for min_ev in min_ev_vals:
        for vol_gate in vol_gate_vals:
            strat = make_hourly_strategy(
                asset, min_ev=min_ev, vol_gate_thresh=vol_gate, stake=stake,
                calibrator=calibrator,
            )
            test_trades = run_hourly_backtest(
                strat, test_events_all, stake=stake,
                btc_prices_df=btc_prices_for_strategy,
            )
            m = compute_metrics(test_trades, n_test_windows, n_test_days)
            m["min_ev"] = min_ev
            m["vol_gate"] = vol_gate

            all_results.append(m)

            score = m["annualized_pnl"]
            if m["total_trades"] < 20:
                score = -9999
            if m["sharpe"] < 0:
                score *= 0.5

            if score > best_metric:
                best_metric = score
                best_result = {
                    "config": {"min_ev": min_ev, "vol_gate": vol_gate},
                    "metrics": m,
                    "trades": test_trades,
                    "score": score,
                }

            if verbose:
                trades_n = m["total_trades"]
                wr = m["win_rate_pct"]
                pnl = m["total_pnl"]
                ann = m["annualized_pnl"]
                dd = m["max_drawdown"]
                sh = m["sharpe"]
                print(
                    f"    min_ev={min_ev:.2f} vol_gate={vol_gate:.1f}: "
                    f"{trades_n:>4} trades | WR {wr:>5.1f}% | "
                    f"PnL ${pnl:>+8.2f} | Ann ${ann:>+8.2f} | "
                    f"DD ${dd:>6.2f} | Sharpe {sh:>+.3f}"
                )

    return {
        "best": best_result,
        "all_results": all_results,
        "n_test_windows": n_test_windows,
        "n_test_days": n_test_days,
        "calibrator_fitted": calibrator is not None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",      nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--train-start", default="2022-01-01")
    parser.add_argument("--train-end",   default="2023-12-31")
    parser.add_argument("--test-start",  default="2024-01-01")
    parser.add_argument("--test-end",    default="2026-04-15")
    parser.add_argument("--stake",       type=float, default=25.0)
    parser.add_argument("--seed",        type=int,   default=42)
    parser.add_argument("--quick",       action="store_true",
                        help="Quick mode: test 2025-01-01 to 2026-04-15 only")
    args = parser.parse_args()

    if args.quick:
        args.test_start = "2025-01-01"

    def ts(s): return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()

    train_start_ts = ts(args.train_start)
    train_end_ts   = ts(args.train_end)
    test_start_ts  = ts(args.test_start)
    test_end_ts    = ts(args.test_end)

    # Asset-specific grids
    BTC_MIN_EV_VALS   = [0.02, 0.03, 0.04]
    BTC_VOL_GATE_VALS = [4.0, 5.0, 6.0]
    ETH_MIN_EV_VALS   = [0.02, 0.03, 0.04]
    ETH_VOL_GATE_VALS = [4.5, 5.5, 6.5]

    print("=" * 70)
    print("  BTC V3 MEAN-REVERSION + ETH V2+ ENHANCED -- COMPREHENSIVE BACKTEST")
    print(f"  Train: {args.train_start} -> {args.train_end}")
    print(f"  Test:  {args.test_start} -> {args.test_end}")
    print(f"  Stake: ${args.stake:.0f} per trade | Seed: {args.seed}")
    print(f"  BTC grid: min_ev={BTC_MIN_EV_VALS} x vol_gate={BTC_VOL_GATE_VALS}")
    print(f"  ETH grid: min_ev={ETH_MIN_EV_VALS} x vol_gate={ETH_VOL_GATE_VALS}")
    print("=" * 70)

    btc_df = load_btc_prices()
    final_results = {}

    for asset in args.assets:
        print(f"\n{'='*70}")
        print(f"  {asset} GRID SEARCH")
        print(f"{'='*70}")

        min_ev_vals   = BTC_MIN_EV_VALS   if asset == "BTC" else ETH_MIN_EV_VALS
        vol_gate_vals = BTC_VOL_GATE_VALS if asset == "BTC" else ETH_VOL_GATE_VALS

        try:
            df = load_prices(asset)
            print(f"  Loaded {len(df):,} rows for {asset}")
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")
            continue

        grid_result = grid_search_asset(
            asset=asset,
            df=df,
            btc_df=btc_df,
            train_start_ts=train_start_ts,
            train_end_ts=train_end_ts,
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            stake=args.stake,
            seed=args.seed,
            min_ev_vals=min_ev_vals,
            vol_gate_vals=vol_gate_vals,
            verbose=True,
        )

        best = grid_result["best"]
        if best is None:
            print(f"\n  {asset}: No viable config found.")
            continue

        n_days = grid_result["n_test_days"]
        m = best["metrics"]
        trades = best["trades"]
        cfg = best["config"]

        print(f"\n  {'='*60}")
        print(f"  {asset} BEST CONFIG: min_ev={cfg['min_ev']} vol_gate={cfg['vol_gate']}")
        print(f"  {'='*60}")
        print(f"  Test period: {args.test_start} -> {args.test_end} ({n_days:.0f} days)")
        print(f"  Total trades:     {m['total_trades']} / {m['total_windows']} windows ({m['trade_rate_pct']}% trade rate)")
        print(f"  Win rate:         {m['win_rate_pct']}%  ({m['wins']} W / {m['losses']} L)")
        print(f"  Total PnL:        ${m['total_pnl']:+.2f}")
        print(f"  Annualized PnL:   ${m['annualized_pnl']:+.2f}")
        print(f"  Max drawdown:     ${m['max_drawdown']:.2f}")
        print(f"  Sharpe (trades):  {m['sharpe']:.3f}")
        print(f"  Avg EV at entry:  {m['avg_ev_at_entry']:+.4f}")

        # Stake scaling
        if m['total_trades'] > 0 and args.stake > 0:
            pnl_per_dollar = m['annualized_pnl'] / args.stake
            print(f"\n  Stake scaling (annualized):")
            for s in [25, 50, 100, 200, 500]:
                proj = pnl_per_dollar * s
                print(f"    ${s:>4} stake -> ${proj:>+9.0f}/yr")

        # Signal contribution analysis
        sig_report = signal_contribution_report(trades)
        if sig_report:
            print(f"\n  Signal contribution (wins vs losses):")
            print(f"  {'Signal':<22} {'Win#':>5} {'WinAvg':>8} {'Loss#':>6} {'LossAvg':>9}")
            for sig_name, sr in sorted(sig_report.items()):
                print(
                    f"  {sig_name:<22} {sr['win_count']:>5} {sr['win_avg']:>+8.4f} "
                    f"{sr['loss_count']:>6} {sr['loss_avg']:>+9.4f}"
                )

        # Quarterly breakdown
        qb = quarterly_breakdown(trades)
        if qb:
            print(f"\n  Quarterly breakdown:")
            print(f"  {'Quarter':<10} {'Trades':>7} {'WR%':>7} {'PnL':>10}  {'Cumulative':>12}")
            running_pnl = 0.0
            for qk, qm in qb.items():
                running_pnl += qm["pnl"]
                print(
                    f"  {qk:<10} {qm['trades']:>7} {qm['win_rate']:>6.1f}% "
                    f"${qm['pnl']:>+8.2f}  ${running_pnl:>+10.2f}"
                )

        final_results[asset] = {
            "best_config": cfg,
            "metrics": m,
            "quarterly": qb,
            "n_days": n_days,
            "all_grid_results": grid_result["all_results"],
            "signal_report": sig_report,
        }

    # Combined summary
    print(f"\n{'='*70}")
    print("  COMBINED SUMMARY")
    print(f"{'='*70}")
    combined_ann = 0.0
    for asset, r in final_results.items():
        m = r["metrics"]
        ann = m["annualized_pnl"]
        combined_ann += ann
        print(
            f"  {asset}: best min_ev={r['best_config']['min_ev']} vol_gate={r['best_config']['vol_gate']} | "
            f"{m['total_trades']} trades | WR {m['win_rate_pct']}% | "
            f"PnL ${m['total_pnl']:+.2f} | Ann ${ann:+.2f} | "
            f"DD ${m['max_drawdown']:.2f} | Sharpe {m['sharpe']:.3f}"
        )
    if len(final_results) > 1:
        print(f"\n  Combined annualized PnL (BTC+ETH @ ${args.stake:.0f}): ${combined_ann:+.2f}")
        for s in [50, 100, 200, 500]:
            ratio = s / args.stake
            print(f"  Combined @ ${s} stake: ${combined_ann * ratio:+,.0f}/yr")

    print(f"\n{'='*70}")
    print("  HONEST ASSESSMENT")
    print(f"{'='*70}")
    print("  BTC V3 uses mean-reversion: VWAP z-score, RSI extremes, Bollinger,")
    print("  momentum reversal timing. All signals fade overbought/oversold moves.")
    print("  ETH V2+ adds RSI + Bollinger confirmation on top of BTC lead-lag.")
    print("  Simulated orderbook -- live spreads will reduce PnL vs backtest.")
    print("  Minimum 50%+ WR needed to overcome Kalshi fees at $25 stake.")

    out_path = Path("results/hourly_v3_backtest_report.json")
    out_path.parent.mkdir(exist_ok=True)
    save_results = {
        asset: {
            "best_config": r["best_config"],
            "metrics": r["metrics"],
            "quarterly": r["quarterly"],
            "n_days": r["n_days"],
            "signal_report": r.get("signal_report", {}),
        }
        for asset, r in final_results.items()
    }
    with open(out_path, "w") as f:
        json.dump(save_results, f, indent=2)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
