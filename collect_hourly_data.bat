@echo off
setlocal
cd /d %~dp0

:: ============================================================
:: COLLECT KALSHI HOURLY LADDER HISTORY  (BTC + ETH)
::
:: Uses --candlesticks mode so each market has real per-minute
:: bid/ask history during its life (required for a meaningful
:: Strategy C backtest).
::
:: --days 7 -> ~25 min per asset. The collector is resumable
:: (skips event parquets already on disk), so you can rerun
:: this with a higher --days later to extend history overnight.
:: ============================================================

echo [1/2] Collecting BTC hourly ladder history (last 7 days) ...
py collect_kalshi_ladder_history.py --asset btc --days 7 --candlesticks
if errorlevel 1 goto :error

echo.
echo [2/2] Collecting ETH hourly ladder history (last 7 days) ...
py collect_kalshi_ladder_history.py --asset eth --days 7 --candlesticks
if errorlevel 1 goto :error

echo.
echo Done. Output in data\kalshi\hourly\BTC\ and data\kalshi\hourly\ETH\
echo Next step: double-click run_backtests_hourly.bat
pause
exit /b 0

:error
echo.
echo Collection failed. See messages above.
pause
exit /b 1
