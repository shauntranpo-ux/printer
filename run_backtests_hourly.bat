@echo off
setlocal
set OMP_NUM_THREADS=1
set OPENBLAS_NUM_THREADS=1
set MKL_NUM_THREADS=1
set NUMEXPR_NUM_THREADS=1

cd /d %~dp0

:: ============================================================
:: HOURLY STRATEGY C — BTC + ETH ONLY
:: Requires: data/kalshi/hourly/BTC/ and data/kalshi/hourly/ETH/
::           populated with Kalshi ladder parquet files.
:: ============================================================

echo [1/3] Training Strategy C (BTC + ETH)...
py backtesting/cli.py train --asset btc --strategy c
py backtesting/cli.py train --asset eth --strategy c

echo [2/3] Running Strategy C backtests...
py backtesting/cli.py backtest --asset btc --strategy c
py backtesting/cli.py backtest --asset eth --strategy c

echo [3/3] Generating Strategy C reports...
py backtesting/cli.py report --asset btc --strategy c
py backtesting/cli.py report --asset eth --strategy c

echo.
echo Done.
pause
