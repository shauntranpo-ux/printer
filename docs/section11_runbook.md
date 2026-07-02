# Section 11 paper-trade runbook

## T+0 (deploy day)

- [ ] Verify Railway paper-validation environment is deployed
- [ ] Verify bot is running (Railway logs show price feeds, no crashes)
- [ ] Run `python scripts\section11_monitor.py --db kalshi_paper_validation.db`
      (may show empty trades table - normal, just confirm DB is reachable)
- [ ] Record deploy timestamp in a note

## Daily (morning + evening check)

- [ ] Check Railway logs for crashes or repeated errors
- [ ] Run monitor script, record running totals to a notebook:
      total_trades, total_pnl, win_rate per asset
- [ ] Verify bot heartbeat (timestamp of most recent log line
      5 minutes ago)
- [ ] Check Kalshi account for unexpected orders (safety check -
      paper mode should NEVER place real orders)

## T+7 (mid-point check)

- [ ] Does any asset already have 20+ trades? Note which.
- [ ] Is any asset showing catastrophic loss (< -$50 on $5/trade)?
      If yes, pause that asset's trading (don't tune params, just
      stop firing) while investigating.
- [ ] Write a one-paragraph note on what looks interesting. Don't
      make any changes.

## T+14 (decision day)

- [ ] Run monitor script, snapshot final per-asset stats
- [ ] Compare each asset's stats against docs/section11_decision_framework.md
- [ ] Write a decision note per asset: go-live / extend-paper / abandon
- [ ] Report findings back; proceed to Section 12 (fix backtest) or
      to Section 14 (staged go-live for qualifying assets) depending
      on results

## If something breaks

- **Bot crashes repeatedly**: check Railway logs for the traceback.
  Common causes: Solana RPC rate-limiting (non-fatal, will cache),
  Kalshi API token expiry, database lock contention. Fix and redeploy
  - do NOT lose the accumulated paper data by wiping the DB.

- **Bot stops firing trades for 24+ hours across all assets**: check
  Kalshi API connectivity, check config flags, check price feed health.
  If the bot is healthy but skipping everything, Min EV may still be
  too tight - NOTE for Section 12, don't fix in-flight.

- **Unexpected live trade detected**: EMERGENCY. Pause the bot
  immediately (Railway UI: stop the service). Reconcile Kalshi account
  manually. Before restart, add defensive check in the order-submit
  code path.
