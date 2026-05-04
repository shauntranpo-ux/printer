# backtesting/research_cli.py
"""
Entry point: python backtesting/research_cli.py --asset BTC [--layers 1,2,3,4,5]

Loads 1-min bars for the asset, runs requested layers, writes report to
backtesting/output/research/{asset}/.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

# Ensure UTF-8 stdout on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf-8-sig'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Ensure project imports work when called directly
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'src'))

import pandas as pd
from backtesting.data.loaders import load_bars
from backtesting.research.label_builder import STRIKE_SPACING, nearest_strike
from backtesting.research.layer1 import run_layer1
from backtesting.research.layer1_real import load_settlements, run_layer1_real
from backtesting.research.layer2 import run_layer2
from backtesting.research.layer3 import run_layer3
from backtesting.research.layer4 import run_layer4
from backtesting.research.layer5 import run_layer5
from backtesting.research.report_writer import write_research_report


def _count_trials(asset: str) -> int:
    """Estimate number of independent configs tested from EV sweep files."""
    sweep_dir = _ROOT / 'backtesting' / 'output'
    pattern = f'ev_wfa_{asset.lower()}_ev*.csv'
    files = list(sweep_dir.glob(pattern))
    return max(len(files), 5)  # minimum 5


def main():
    parser = argparse.ArgumentParser(description='5-layer backtest validation')
    parser.add_argument('--asset', required=True, choices=['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'])
    parser.add_argument('--layers', default='1,2,3,4,5', help='Comma-separated layer numbers')
    parser.add_argument('--iters', type=int, default=1000, help='Permutation iterations (layer 4)')
    parser.add_argument('--use-real-settlements', action='store_true',
                        help='Use real Kalshi YES/NO outcomes for Layer 1 IC (requires settlements parquet)')
    args = parser.parse_args()

    layers = [int(x.strip()) for x in args.layers.split(',')]
    asset  = args.asset.upper()
    output_dir = _ROOT / 'backtesting' / 'output' / 'research' / asset
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f'[research] Loading bars for {asset}...')
    bars = load_bars(asset)
    if bars.empty:
        sys.exit(f'[research] ERROR: no bar data found for {asset}')

    # Use median price as ATM strike approximation (for synthetic-label layers)
    strike = nearest_strike(float(bars['close'].median()), asset)
    print(f'[research] Strike (ATM approx): {strike}')

    results = {}

    if 1 in layers:
        print('[research] Running Layer 1 - Signal IC...')
        if args.use_real_settlements:
            try:
                settlements = load_settlements(asset)
                t0 = settlements['window_open'].min().date()
                t1 = settlements['window_open'].max().date()
                print(f'[research] Real settlements: {len(settlements):,} windows ({t0} -> {t1})')
                results['layer1'] = run_layer1_real(bars, settlements, asset=asset)
                r1 = results['layer1']
                print(f"  -> {r1['verdict']} ({r1['n_failing']}/{r1['n_signals']} failing, "
                      f"{r1.get('n_windows', '?')} windows, {r1.get('n_skipped', '?')} skipped)")
            except FileNotFoundError as exc:
                print(f'[research] WARNING: {exc}')
                print('[research] Falling back to synthetic labels...')
                results['layer1'] = run_layer1(bars, strike=strike, asset=asset)
                print(f"  -> {results['layer1']['verdict']} ({results['layer1']['n_failing']} failing signals)")
        else:
            results['layer1'] = run_layer1(bars, strike=strike, asset=asset)
            print(f"  -> {results['layer1']['verdict']} ({results['layer1']['n_failing']} failing signals)")

    # Build trade log for layers 2-4
    trade_log: pd.DataFrame | None = None

    if 2 in layers:
        print('[research] Running Layer 2 - Null Hypothesis...')
        wfa_log_path = _ROOT / 'backtesting' / 'output' / f'{asset.lower()}_trades.csv'
        if wfa_log_path.exists():
            trade_log = pd.read_csv(wfa_log_path)
            results['layer2'] = run_layer2(trade_log, asset=asset, n_iter=args.iters)
            print(f"  -> {results['layer2']['verdict']} (p={results['layer2']['p_value']:.4f})")
        else:
            print(f'  [SKIP] No trade log at {wfa_log_path} - run WFA first')
            results['layer2'] = {'verdict': 'SKIPPED', 'reason': 'no_trade_log'}

    if trade_log is None:
        wfa_log_path = _ROOT / 'backtesting' / 'output' / f'{asset.lower()}_trades.csv'
        if wfa_log_path.exists():
            trade_log = pd.read_csv(wfa_log_path)

    if 3 in layers and trade_log is not None:
        print('[research] Running Layer 3 - WFA Significance...')
        import glob
        wfa_files = glob.glob(str(_ROOT / 'backtesting' / 'output' / f'ev_wfa_{asset.lower()}*.csv'))
        wfa_sharpes = []
        for f in wfa_files:
            df = pd.read_csv(f)
            if 'sharpe' in df.columns:
                wfa_sharpes.extend(df['sharpe'].dropna().tolist())
        if hasattr(bars.index, 'freq') or str(bars.index.dtype).startswith('datetime'):
            data_years = max((bars.index[-1] - bars.index[0]).days / 365.25, 0.1)
        elif 'timestamp' in bars.columns:
            t0 = pd.to_datetime(bars['timestamp'].iloc[0])
            t1 = pd.to_datetime(bars['timestamp'].iloc[-1])
            data_years = max((t1 - t0).days / 365.25, 0.1)
        else:
            data_years = 3.0
        results['layer3'] = run_layer3(trade_log, wfa_sharpes=wfa_sharpes,
                                       data_years=data_years, num_trials=_count_trials(asset))
        print(f"  -> {results['layer3']['verdict']} (DSR={results['layer3']['dsr']:.3f})")

    if 4 in layers and trade_log is not None:
        print('[research] Running Layer 4 - Permutation Test...')
        results['layer4'] = run_layer4(trade_log, n_iter=args.iters)
        print(f"  -> {results['layer4']['verdict']} (p_block={results['layer4']['p_value_block']:.4f})")

    if 5 in layers and trade_log is not None:
        print('[research] Running Layer 5 - Regime Robustness...')
        bars_indexed = bars.copy()
        if 'timestamp' in bars_indexed.columns and not isinstance(bars_indexed.index, pd.DatetimeIndex):
            bars_indexed = bars_indexed.set_index(pd.to_datetime(bars_indexed['timestamp'], utc=True))
            bars_indexed.index.name = 'timestamp'
        results['layer5'] = run_layer5(trade_log, bars_indexed)
        print(f"  -> {results['layer5']['verdict']}")

    write_research_report(asset, results, output_dir=output_dir)
    print(f'[research] Report written to {output_dir}/research_report.md')
    overall = max((r.get('verdict', 'FAIL') for r in results.values()),
                  key=lambda v: {'PASS': 0, 'CONDITIONAL': 1, 'FAIL': 2, 'SKIPPED': 1}.get(v, 1))
    print(f'[research] Overall verdict: {overall}')


if __name__ == '__main__':
    main()
