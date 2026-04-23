@echo off
setlocal
set OMP_NUM_THREADS=1
set OPENBLAS_NUM_THREADS=1
set MKL_NUM_THREADS=1
set NUMEXPR_NUM_THREADS=1

cd /d %~dp0

:: ============================================================
:: SECTION 1 - TRAIN REMAINING MODELS (SOL/B, XRP/A, XRP/B)
:: ============================================================
echo [1/3] Training remaining models...
py backtesting/cli.py train --asset sol --strategy b
py backtesting/cli.py train --asset xrp --strategy a
py backtesting/cli.py train --asset xrp --strategy b

:: ============================================================
:: SECTION 2 - RUN BACKTESTS (all 8 instances)
:: ============================================================
echo [2/3] Running backtests...
py backtesting/cli.py backtest --asset btc --strategy a
py backtesting/cli.py backtest --asset btc --strategy b
py backtesting/cli.py backtest --asset eth --strategy a
py backtesting/cli.py backtest --asset eth --strategy b
py backtesting/cli.py backtest --asset sol --strategy a
py backtesting/cli.py backtest --asset sol --strategy b
py backtesting/cli.py backtest --asset xrp --strategy a
py backtesting/cli.py backtest --asset xrp --strategy b

:: ============================================================
:: SECTION 3 - GENERATE REPORTS (all 8 instances)
:: ============================================================
echo [3/3] Generating reports...
py backtesting/cli.py report --asset btc --strategy a
py backtesting/cli.py report --asset btc --strategy b
py backtesting/cli.py report --asset eth --strategy a
py backtesting/cli.py report --asset eth --strategy b
py backtesting/cli.py report --asset sol --strategy a
py backtesting/cli.py report --asset sol --strategy b
py backtesting/cli.py report --asset xrp --strategy a
py backtesting/cli.py report --asset xrp --strategy b

echo.
echo Done. Drop results here and I will apply best strategy per market and push.

:: ============================================================
:: SECTION 4 - STRATEGY C (BTC + ETH HOURLY STRIKE-LADDER)
:: NOTE: Requires data/kalshi/hourly/BTC/ and data/kalshi/hourly/ETH/
::       to be populated with Kalshi ladder parquet files first.
::       Uncomment these lines once ladder data is available.
:: ============================================================

:: -- Strategy C: Train (fits HAR, calibrators, writes fitted.yaml)
:: py backtesting/cli.py train --asset btc --strategy c
:: py backtesting/cli.py train --asset eth --strategy c

:: -- Strategy C: Validate (event-level CPCV)
:: py backtesting/cli.py validate --asset btc --strategy c
:: py backtesting/cli.py validate --asset eth --strategy c

:: -- Strategy C: Backtest (simulation)
:: py backtesting/cli.py backtest --asset btc --strategy c
:: py backtesting/cli.py backtest --asset eth --strategy c

:: -- Strategy C: Reports
:: py backtesting/cli.py report --asset btc --strategy c
:: py backtesting/cli.py report --asset eth --strategy c

:: -- Strategy C: Full pipeline (train + validate + backtest + report)
:: py backtesting/cli.py all --asset btc --strategy c
:: py backtesting/cli.py all --asset eth --strategy c
