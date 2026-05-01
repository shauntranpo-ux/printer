@echo off
setlocal
cd /d %~dp0

:: Tops up data\BTC_1m.csv and data\ETH_1m.csv with the latest Binance
:: 1-minute klines. Resumable (skips rows already on disk).

echo Updating BTC + ETH 1-minute bars from Binance ...
py download_data.py --asset BTC ETH
if errorlevel 1 goto :error

echo.
echo Done. Now re-run run_backtests.bat
pause
exit /b 0

:error
echo.
echo Update failed. See messages above.
pause
exit /b 1
