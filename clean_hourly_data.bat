@echo off
setlocal
cd /d %~dp0

:: Removes any previously collected hourly Kalshi ladder parquet files
:: so a fresh collection run starts clean.

echo Deleting data\kalshi\hourly\BTC and data\kalshi\hourly\ETH ...
if exist "data\kalshi\hourly\BTC" rmdir /s /q "data\kalshi\hourly\BTC"
if exist "data\kalshi\hourly\ETH" rmdir /s /q "data\kalshi\hourly\ETH"

echo.
echo Done.
pause
