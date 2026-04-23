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

---

## Strategy C — Kalshi Hourly Strike-Ladder (BTC + ETH only)

Strategy C models Kalshi hourly binary options as a strike ladder forming an empirical risk-neutral CDF. Two sub-strategies run independently per event:

- **C1**: Per-strike probability surface model (HAR-RS-J volatility forecast + N(d₂) digital call + per-moneyness IsotonicRegression calibration)
- **C2**: Model-free ladder no-arbitrage scanner (monotonicity, convexity, bounds violations)

### Additional directory structure

```
backtesting/
├── training/
│   └── strategy_c_fitter.py          # fit_strategy_c() — HAR + calibrators + fitted.yaml
├── validation/
│   └── strategy_c_cpcv_adapter.py    # event-level CPCV (all strikes per event in same fold)
├── metrics/
│   └── strike_ladder_metrics.py      # per-moneyness calibration, per-event P&L, C2 summary
├── simulation/
│   └── strategy_c_adapter.py         # run_strategy_c_backtest() — event-driven C1+C2 loop
└── reports/
    └── templates/
        ├── strategy_c_asset_report.md.j2
        └── strategy_c_comparison.md.j2
```

Strategies and per-asset configs live under `strategies/strategy_c/` (not in the backtesting layer).

### Data requirements

Kalshi hourly ladder history must exist at:
```
data/kalshi/hourly/BTC/   ← one or more parquet files per event
data/kalshi/hourly/ETH/
```

Required columns per row: `event_id`, `event_close_time` (UTC), `timestamp` (UTC), `strike`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `mid_price`, `volume`, `market_id`.

### Strategy C CLI commands

```bash
# Train Strategy C for BTC
python backtesting/cli.py train --asset btc --strategy c

# Event-level CPCV validation
python backtesting/cli.py validate --asset btc --strategy c

# Run simulation
python backtesting/cli.py backtest --asset btc --strategy c

# Generate reports
python backtesting/cli.py report --asset btc --strategy c

# Full pipeline
python backtesting/cli.py all --asset btc --strategy c
```

### Key differences from Strategies A/B

| Property | Strategies A/B | Strategy C |
|----------|---------------|------------|
| Market | 15-min binary up/down | Hourly strike-ladder (40 strikes/event) |
| Assets | BTC, ETH, SOL, XRP | BTC, ETH only |
| CPCV split unit | 15-min bar | Kalshi event (all strikes together) |
| Embargo | 30 minutes | 1 hour |
| Position cap | 1 per window | 2 per event (C1+C2 combined) |
| Fee model | 3% taker | 3% taker |

### Hard constraints (in addition to global constraints)

6. **BTC and ETH only.** Strategy C never runs for SOL or XRP. The code raises `ValueError` if another asset is passed.

7. **Event-level CPCV.** All ~40 strikes for a single Kalshi event must be assigned to the same fold. Row-level splits are forbidden.

8. **2-position cap per event.** C1 and C2 combined may not exceed 2 positions per event. The simulation adapter enforces this cap in `run_strategy_c_backtest()`.

9. **Sidecar config only.** Fitted artifacts are written to `strategies/strategy_c/config/{asset}.fitted.yaml`. The original `{asset}.yaml` is never modified.
