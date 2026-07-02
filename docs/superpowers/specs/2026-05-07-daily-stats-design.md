# Daily Stats & Telegram Cleanup Design
Date: 2026-05-07

## Goal
Replace noisy per-trade Telegram notifications with a clean daily summary push.
Add a terminal stats script for on-demand inspection.

## Scope
- Remove per-trade Telegram pings (order placed/filled/closed for S1 and S2)
- Keep critical alerts (startup, loss limits, consecutive losses, preflight, crash)
- New `bot_stats.py` module: query DB + format stats
- New `scripts/stats_report.py`: terminal output
- `bot_loops.py`: midnight trigger sends daily summary via Telegram

---

## Files

| Action  | Path                      | Responsibility                              |
|---------|---------------------------|---------------------------------------------|
| Create  | `bot_stats.py`            | DB queries + format (Telegram + terminal)   |
| Create  | `scripts/stats_report.py` | CLI wrapper - prints stats to stdout        |
| Modify  | `bot_loops.py`            | Midnight trigger + import bot_stats         |
| Modify  | `bot_market.py`           | Remove 3 per-trade send_telegram calls      |
| Modify  | `bot_loops.py`            | Remove 2 per-trade send_telegram calls      |
| Modify  | `bot_risk.py`             | Remove 2 per-trade send_telegram calls      |

---

## bot_stats.py

### `query_stats(db_path: str, today_date: str | None = None) -> dict`

Runs two queries:

**Today query** (UTC date = today_date or current UTC date):
```sql
SELECT strategy_variant, asset, outcome, COUNT(1) as n, SUM(pnl_dollars) as pnl
FROM trades
WHERE DATE(ts) = :today AND outcome IN ('win', 'loss')
GROUP BY strategy_variant, asset, outcome
```

**All-time query**:
```sql
SELECT COUNT(1) as n,
       SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
       SUM(pnl_dollars) as pnl
FROM trades
WHERE outcome IN ('win', 'loss')
```

**Last trade query**:
```sql
SELECT ts FROM trades WHERE outcome IN ('win','loss') ORDER BY ts DESC LIMIT 1
```

Returns:
```python
{
    "date":               "2026-05-07",
    "today_trades":       12,
    "today_wins":         8,
    "today_losses":       4,
    "today_pnl":          47.20,
    "alltime_trades":     90,
    "alltime_wins":       57,
    "alltime_pnl":        105.93,
    "by_strategy_asset":  {
        # (strategy_variant, asset) -> {"wins": N, "losses": N, "pnl": X}
        ("strategy1", "BTC"): {"wins": 3, "losses": 1, "pnl": 12.50},
        ("strategy2", "BTC"): {"wins": 4, "losses": 2, "pnl": 26.00},
    },
    "last_trade_ts":      "2026-05-07T14:30:00+00:00",  # None if no trades ever
    "consecutive_losses": 0,   # read from bot_state._consecutive_losses (passed in)
    "mode":               "PAPER",
}
```

### `format_telegram(stats: dict) -> str`

Returns HTML string using `<b>` and `<code>` tags. Strategy label map:
- `"strategy1"` -> `"S1 * EMA Momentum"`
- `"strategy2"` -> `"S2 * Contract Velocity"`

Asset rows with zero trades today are hidden.
Strategy sections with zero trades today are hidden.
"No trades today" shown in the body if today_trades == 0.

```
 Daily Summary - 2026-05-07
───────────────────────────
Trades: 12  |  WR: 8/12 (66.7%)
P&L today: +$47.20  |  All-time: +$105.93
───────────────────────────
S1 * EMA Momentum
  BTC   3W/1L   +$12.50
  ETH   1W/0L   +$8.70
S2 * Contract Velocity
  BTC   4W/2L   +$26.00
  SOL   0W/1L   -$3.70
───────────────────────────
Last trade: 14 min ago
Consecutive losses: 0
Mode: PAPER
```

"Last trade: N min ago" - computed from `last_trade_ts` vs current time.
If last_trade_ts is None: "Last trade: never".
If > 24h: show hours.

### `format_terminal(stats: dict) -> str`

Same content, plain text (no HTML tags). Used by `scripts/stats_report.py`.

---

## scripts/stats_report.py

Standalone script, no bot_state import at module level.

```
Usage: py scripts/stats_report.py [--date YYYY-MM-DD]
```

Reads `BOT_DB_FILE` env var (defaults to `kalshi_bot.db`).
Calls `query_stats()` + `format_terminal()`, prints to stdout.
Passes `consecutive_losses=0` (not available outside running bot).

---

## bot_loops.py - Midnight Trigger

Module-level variable:
```python
_last_stats_date: str = ""
```

At the top of each `main_loop` iteration (after sleep), before market logic:
```python
_today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
if _today != _last_stats_date:
    _last_stats_date = _today
    _stats = bot_stats.query_stats(bot_state._DB_FILE, today_date=_last_stats_date)
    _stats["consecutive_losses"] = bot_state._consecutive_losses
    _stats["mode"] = read_config().get("mode", "paper").upper()
    asyncio.create_task(send_telegram(bot_stats.format_telegram(_stats)))
```

Only fires once per UTC day. Bot does not need to be running at exactly midnight -
fires on the first tick after date changes.

---

## Telegram Removals

### bot_market.py (3 removals)
- `send_telegram("...MARKET ORDER PLACED...")` - line ~974
- `send_telegram("...MARKET ORDER FAILED...")` - line ~1049
- `send_telegram("...MARKET ORDER NOT FILLED...")` - line ~1175

### bot_loops.py (2 removals)
- `send_telegram("...S2 D3 Hybrid...ORDER FILLED...")` - line ~529
- `send_telegram("...S2 D3 Hybrid...outcome...")` - line ~680

### bot_risk.py (2 removals)
- `send_telegram("...S1 Original...ORDER FILLED...")` - line ~454
- `send_telegram("...S1 Original...outcome...")` - line ~511

### Kept (critical alerts only)
- `bot.py:114` - startup message
- `bot_infra.py:462` - fill verification warning
- `bot_loops.py:663` - consecutive losses warning
- `bot_risk.py:98,107` - daily loss limit triggered
- `bot_risk.py:785` - preflight failed
- `runner.py:134,272,297` - crash loop / preflight failures

---

## Error Handling
- DB unavailable: log warning, skip stats send (do not crash bot)
- Telegram send fails: existing retry logic in `send_telegram` handles it
- No trades ever: "Last trade: never", zeros for all counts

---

## Testing
- Unit tests for `query_stats` with an in-memory SQLite DB
- Unit tests for `format_telegram` output shape (strategy sections hidden when zero trades)
- Unit test for midnight trigger: date changes -> stats sent exactly once
