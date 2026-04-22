# Backtesting Layer

Offline backtesting framework for the Kalshi 15-minute crypto up/down strategy. Given historical OHLCV bars and Kalshi tick data, it fits HAR-RV + logistic models, runs a realistic order-book simulation, and produces calibration, equity-curve, regime, and overfitting reports — all without touching live exchange connections.

---

## Directory structure

```
backtesting/
├── cli.py                        # Entry point — all commands start here
├── configs/
│   ├── backtest.yaml             # Global config (latency, output dirs, asset list)
│   └── per_asset/                # Per-asset overrides (btc.yaml, eth.yaml, …)
├── data/
│   ├── loaders.py                # load_bars(), load_kalshi_ticks(), load_l2_snapshots()
│   ├── label_builder.py          # build_labels() — 15-min forward log-return labels
│   └── aligner.py                # build_event_stream(), iter_windows()
├── training/
│   ├── har_fitter.py             # HAR-RV volatility model
│   ├── model_fitter.py           # Logistic classifier (strategy A/B)
│   └── pipeline.py               # run_training_pipeline() — orchestrates per-asset fits
├── validation/
│   ├── cpcv.py                   # Combinatorial Purged Cross-Validation
│   ├── lookahead_checker.py      # Detects feature leakage before any fit
│   ├── stationary_bootstrap.py   # Block-bootstrap confidence intervals
│   └── walk_forward.py           # Walk-forward analysis adapter
├── metrics/
│   ├── calibration.py            # Brier score, reliability diagram data
│   ├── trading.py                # Sharpe, max-DD, win-rate, Kelly
│   ├── overfitting.py            # IS/OOS Sharpe ratio, deflated Sharpe test
│   └── regime_filters.py         # BTC-vol tercile regime tagging
├── simulation/
│   ├── fill_model.py             # Taker/maker fill probability models
│   └── backtest_engine.py        # run_backtest() — event-driven P&L loop
├── reports/
│   ├── report_builder.py         # build_asset_report(), render_asset_report()
│   ├── comparison.py             # build_comparison_report() — cross-asset summary
│   └── templates/                # Jinja2 Markdown templates
└── output/
    ├── models/                   # Serialised fitted models (joblib)
    └── reports/                  # Generated PNG charts + JSON + report.md
```

---

## CLI commands

Run all commands from the **project root** (`C:\Users\alxnt\kalshi-bot`).

```bash
# End-to-end dry run on synthetic data — no real data files required
python backtesting/cli.py dry-run --asset btc --strategy a

# Train HAR + classifier models for one asset
python backtesting/cli.py train --asset btc

# Train all assets (omit --asset)
python backtesting/cli.py train

# Run the backtest engine for one asset / strategy
python backtesting/cli.py backtest --asset eth --strategy a

# Generate reports from a completed backtest
python backtesting/cli.py report --asset eth --strategy a

# Run full train → backtest → report pipeline for one asset
python backtesting/cli.py all --asset sol --strategy b
```

### Strategy choices
| Value | Description |
|-------|-------------|
| `a`   | Strategy A — base logistic model |
| `b`   | Strategy B — alternative feature set |

---

## Config files

### `backtesting/configs/backtest.yaml` — global settings

Key sections to edit:

```yaml
data:
  base_path: data/historical      # path to OHLCV parquet files

simulation:
  latency_ms: 500                 # order-submission latency

reports:
  output_dir: backtesting/output/reports

execution:
  assets: [btc, eth, sol, xrp]   # assets to iterate over
  strategies: [a, b]
  sequential_only: true           # never change — see constraints below
```

### `backtesting/configs/per_asset/<asset>.yaml` — per-asset overrides

Extend or override any `backtest.yaml` key for a specific asset.

---

## Hard constraints

1. **Sequential only.** No `async`, no `threading`, no `multiprocessing` — anywhere in this layer. The `execution.sequential_only: true` flag is a documentation marker; enforcement is by code review.

2. **No data pooling across assets.** Models are fit independently per asset. Cross-asset features are forbidden to prevent look-ahead contamination.

3. **3 % fee model.** Kalshi charges a 3 % fee on winnings. All P&L calculations in `simulation/backtest_engine.py` apply this fee before computing Sharpe and Kelly statistics.

4. **Embargo on label windows.** CPCV purges samples within 30 minutes of a test boundary to prevent leakage from overlapping 15-minute windows.

5. **Dry-run is the acceptance gate.** Before any live deployment, `python backtesting/cli.py dry-run` must complete without assertion errors and produce all 8 report artifacts.
