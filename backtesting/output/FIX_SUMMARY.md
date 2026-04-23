# Fix Summary

## Files Modified

| File | Change |
|------|--------|
| `backtesting/data/loaders.py` | Rewrote path resolution (flat-file convention), epoch-seconds timestamp handling, L2/trades/funding return empty DataFrames with warning instead of raising |
| `backtesting/configs/backtest.yaml` | Added `historical_root`, `bars_filename_pattern`, `granularity_seconds: 60`, `bars_per_15min_window: 15` under `data:` |
| `backtesting/configs/per_asset/btc.yaml` | Added `granularity_seconds: 60` |
| `backtesting/configs/per_asset/eth.yaml` | Added `granularity_seconds: 60` |
| `backtesting/configs/per_asset/sol.yaml` | Added `granularity_seconds: 60` |
| `backtesting/configs/per_asset/xrp.yaml` | Added `granularity_seconds: 60` |
| `backtesting/data/label_builder.py` | Replaced hardcoded `expected_bars = 90` with auto-detection from median inter-bar interval; added tolerance floor (`bars_per_window - 1`); added `bars_per_window` parameter |
| `backtesting/training/har_fitter.py` | Added `import logging`; added warning when `granularity_seconds >= 60` |
| `backtesting/training/pipeline.py` | Added `_detect_granularity()` helper; passes detected granularity to `build_har_features` and `fit_har_rsj`; added `order_flow.enabled` check with log message |
| `backtesting/cli.py` | Added `--strategy {a,b,both}` to `train`; added `validate` subcommand with `--asset`, `--strategy`, `--method`, `--config`; updated `all` to chain train → validate → backtest → report |

## Files Created

| File | Content |
|------|---------|
| `strategies/strategy_a/config/btc.fitted.yaml` | Sidecar with `returns.granularity_seconds: 60`, `order_flow.enabled: false`, null HAR coefficients |
| `strategies/strategy_a/config/eth.fitted.yaml` | Same as BTC sidecar |
| `strategies/strategy_a/config/sol.fitted.yaml` | Same as BTC sidecar |
| `strategies/strategy_a/config/xrp.fitted.yaml` | Same as BTC sidecar |

## Config Fields Added or Changed

| Field | Value | File |
|-------|-------|------|
| `data.historical_root` | `data/historical` | `backtest.yaml` |
| `data.bars_filename_pattern` | `{asset_upper}_1m_extended.parquet` | `backtest.yaml` |
| `data.granularity_seconds` | `60` | `backtest.yaml` |
| `data.bars_per_15min_window` | `15` | `backtest.yaml` |
| `data.base_path` (removed) | — | `backtest.yaml` |
| `granularity_seconds` | `60` | all four `per_asset/*.yaml` |
| `returns.granularity_seconds` | `60` | all four `*.fitted.yaml` sidecars |
| `order_flow.enabled` | `false` | all four `*.fitted.yaml` sidecars |

## Strategy Layer

No files under `strategies/` were modified. The four `.fitted.yaml` sidecar files are training artifacts written by `backtesting.training.har_fitter.write_fitted_config` — they are distinct from the original strategy configs (`btc.yaml`, `eth.yaml`, `sol.yaml`, `xrp.yaml`).

## Dry-Run Outcome

**PASSED**

```
INFO Synthetic bars: 60480, labels: 672
INFO Trades: 0
INFO Dry run PASSED. Report: backtesting/output/reports\btc\report.md
```

**Report artifact:** `backtesting/output/reports/btc/report.md`

**All 8 artifacts produced:**
- `calibration_chart.png` (26,667 bytes)
- `equity_curve.png` (13,809 bytes)
- `regime_heatmap.png` (10,908 bytes)
- `cpcv_splits.png` (11,105 bytes)
- `bootstrap_ci.png` (11,809 bytes)
- `calibration_summary.json` (69 bytes)
- `trading_summary.json` (129 bytes)
- `overfitting_summary.json` (51 bytes)

**All 19 existing tests passed** (test_data_loaders, test_label_builder, test_cli — no regressions).

---

Ready for full sequential run
