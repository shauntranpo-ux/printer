# Backtest Report
Generated: 2026-04-18T11:54:49.841877+00:00

## Methodology

- Data: 1-min OHLCV from Binance, 2020-present
- Windows: 15-min binary contracts, strike = round-to-increment of window-open price
- Fills: entry at simulated orderbook ask, conservative spread/noise model
- Fees: exact Kalshi formula (ceil(0.07 * C * P * (1-P)) taker)
- Calibration: isotonic regression fit on train, locked for test
- Purge: 1-day gap between train and test to prevent window-boundary leakage

## Walk-forward results

- train days: 90  test days: 30  purge: 1
- period: 2023-01-01 to 2025-10-01T00:00:00+00:00

- **BTC**: trades=  32  win=0.562  pnl=$-16.50  avg=$-0.5156  sharpe=-0.13  brier=0.276
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

## Out-of-sample holdout (the honest number)

Holdout window: 2025-10-01 to 2026-04-18T11:25:46.725514+00:00

- **BTC**: trades=   6  win=0.667  pnl=$-1.27  avg=$-0.2110  sharpe=-0.06  brier=0.374
  - calibrated: False  (train trades: 4)
  - max_drawdown: $5.00
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
  - calibrated: False  (train trades: 0)
  - max_drawdown: $0.00
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
  - calibrated: False  (train trades: 0)
  - max_drawdown: $0.00
- **XRP**: trades=   2  win=1.000  pnl=$+1.80  avg=$+0.8982  sharpe=4.42  brier=0.872
  - calibrated: False  (train trades: 0)
  - max_drawdown: $0.00
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
  - calibrated: False  (train trades: 0)
  - max_drawdown: $0.00

## Component ablation

Positive `delta_pnl` means the signal contributes to P&L.

### BTC

Baseline: trades=   4  win=0.500  pnl=$-2.45  avg=$-0.6117  sharpe=-0.12  brier=0.360

| signal | delta_pnl | delta_win_rate |
|--------|-----------|----------------|
| regime_adj | -2.45 | +0.5000 |
| momentum_bias | -2.45 | +0.5000 |
| velocity_adj | +0.00 | +0.0000 |
| bv3_blend | -2.45 | +0.5000 |

### DOGE

Baseline: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

| signal | delta_pnl | delta_win_rate |
|--------|-----------|----------------|
| beta_adj | +0.00 | +0.0000 |
| momentum_bias | +0.00 | +0.0000 |
| velocity_adj | +0.00 | +0.0000 |

### ETH

Baseline: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

| signal | delta_pnl | delta_win_rate |
|--------|-----------|----------------|
| beta_adj | +0.00 | +0.0000 |
| regime_adj | +0.00 | +0.0000 |
| ratio_adj | +0.00 | +0.0000 |
| velocity_adj | +0.00 | +0.0000 |

### SOL

Baseline: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

| signal | delta_pnl | delta_win_rate |
|--------|-----------|----------------|
| beta_adj | +0.00 | +0.0000 |
| momentum_bias | +0.00 | +0.0000 |
| regime_adj | +0.00 | +0.0000 |
| velocity_adj | +0.00 | +0.0000 |
| exhaustion | +0.00 | +0.0000 |

### XRP

Baseline: trades=   1  win=1.000  pnl=$+1.53  avg=$+1.5330  sharpe=0.00  brier=0.706

| signal | delta_pnl | delta_win_rate |
|--------|-----------|----------------|
| beta_adj_cap | +0.00 | +0.0000 |
| regime_adj | +1.53 | +1.0000 |
| news_mode | +0.00 | +0.0000 |
| ratio_adj | +0.00 | +0.0000 |
| velocity_adj | +0.00 | +0.0000 |

## Regime stress tests

### covid_2020

- **BTC**: trades=   4  win=0.500  pnl=$-2.78  avg=$-0.6945  sharpe=-0.14  brier=0.364
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

### crash_2021

- **BTC**: trades=  21  win=0.619  pnl=$-5.68  avg=$-0.2705  sharpe=-0.07  brier=0.316
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

### ftx_2022

- **BTC**: trades=   1  win=1.000  pnl=$+3.96  avg=$+3.9566  sharpe=0.00  brier=0.112
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

### etf_2024

- **BTC**: trades=   3  win=0.667  pnl=$+2.58  avg=$+0.8616  sharpe=0.17  brier=0.274
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

### chop_2026

- **BTC**: trades=   4  win=0.750  pnl=$+1.48  avg=$+0.3702  sharpe=0.10  brier=0.484
- **ETH**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **SOL**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **XRP**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000
- **DOGE**: trades=   0  win=0.000  pnl=$+0.00  avg=$+0.0000  sharpe=0.00  brier=0.000

## Go-live interpretation

Before going live on any asset:

- Holdout `avg_pnl_per_trade > 0`
- Holdout `trade_level_sharpe > 0.5`
- Holdout `max_drawdown` is survivable for your bankroll
- Calibration was fittable (train_trades >= 50)
- Ablation: no signal has materially negative delta_pnl
- Stress: no catastrophic losses in any regime

If ANY condition fails for an asset, do not go live on that asset.
