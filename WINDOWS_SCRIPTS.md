# Windows .bat Scripts

These `.bat` files are Windows-only local dev helpers. They are **not used in Railway deployment** (Railway runs Linux, `start.sh` is the entry point).

`start.bat` is gitignored. The others are committed as dev tooling reference.

| Script | Purpose |
|--------|---------|
| `clean_hourly_data.bat` | Purge stale hourly data files |
| `collect_hourly_data.bat` | Pull and cache hourly market data locally |
| `run_backtests.bat` | Run backtest suite for 15-minute strategies |
| `run_backtests_hourly.bat` | Run backtest suite for hourly strategies |
| `update_underlying_data.bat` | Refresh underlying price data |

**Railway start command:** `bash start.sh` (not `start.bat`)
