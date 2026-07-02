# Window Guard, S1+S2 Dedup, End-of-Session Summary - Design

## Goal

Three independent fixes shipping together: prevent correlated triple-asset fires, prevent double-entry on the same market by S1 and S2, and send a full trading summary notification when quiet hours begin.

## Architecture

All changes are in existing files. No new modules. No new abstractions.

---

## Fix 1: Cross-asset S1 window guard

**Problem:** `_non_btc_asset_loop` processes ETH -> SOL -> XRP sequentially. When all three have pending markets, all three can fire S1 within seconds of each other. These assets are ~0.9 correlated - one macro move hits all three simultaneously. 9 of 155 trades in the dataset were near-simultaneous multi-asset fires.

**Solution:** Add `_s1_window_fired: float = 0.0` to `bot_state.py`. After any non-BTC S1 fill confirms in `_execute_s1_trade` (bot_risk.py), set `bot_state._s1_window_fired = time.time()`. In `strategy_brain_s1` (bot_strategy.py), after the per-asset cap gate, add: if `asset != "BTC"` and `time.time() - bot_state._s1_window_fired < 300`, return skip with reason `s1_window_gate`.

**Why this works without locks:** `_non_btc_asset_loop` uses a sequential `for asset in enabled_assets: await _process_asset(...)`. ETH's entire `_process_asset` call (including `_execute_s1_trade` which `await`s `place_order` and then sets `_s1_window_fired`) completes before SOL's `_process_asset` begins. By the time SOL checks `_s1_window_fired`, ETH has already set it.

**Window duration:** 300 seconds (5 minutes). Configurable via config key `s1_window_guard_secs` (default 300). Covers one full 15-minute candle window with margin.

**BTC excluded:** BTC runs in a separate loop (`main_loop`) and is typically not enabled alongside ETH/SOL/XRP. Guard only applies when `asset != "BTC"`.

---

## Fix 2: S1+S2 same-market dedup

**Problem:** Both `strategy_brain_s1` and `strategy_brain_s2` are called in `handle_ready_phase` for every market. When both return `action: trade` on the same ticker, two orders go to Kalshi for the same market. 6 of 155 trades were double-entries.

**Solution:** In `handle_ready_phase` (bot_loops.py), after `await _execute_s1_trade(...)` and after setting `do_trade = brain["action"] == "trade"`, add:

```python
if do_trade and ticker in bot_state._s1_pending_trades:
    do_trade = False
    skip_reason_ai = "s2_dedup:s1_entered_same_market"
```

`_execute_s1_trade` inserts `ticker` into `bot_state._s1_pending_trades` *before* awaiting `place_order` (race-condition-safe reservation pattern already in the code). So by the time the dedup check runs, the slot is claimed whether or not the fill confirmed yet.

---

## Fix 3: End-of-session summary notification

**Problem:** `_check_daily_stats` only fires at 14h LV (2 PM PT). No notification fires when the bot stops trading at quiet_start_et (default 22h ET). Users have no end-of-day summary.

**Solution:** Track `_prev_quiet: bool` across loop iterations. On each tick, compute `_now_quiet = _is_quiet_hours(config)`. When `_now_quiet and not _prev_quiet` (False->True transition), immediately call `await _check_daily_stats(today)`. Update `_prev_quiet = _now_quiet`.

**Where to add:** Both `main_loop` (BTC loop, inner `while True`) and `_non_btc_asset_loop`. Since `_check_daily_stats` is idempotent per date (guarded by `_last_stats_date`), both loops detecting the transition is safe - only the first detection fires, the second is a no-op.

**2 PM checkpoint unchanged:** The existing `if _now_lv.hour == 14` check stays. Users get a mid-session checkpoint at 2 PM and a final summary when trading stops.

**Content:** Uses existing `bot_stats.format_telegram(_stats)` - full format including P&L, WR, trade count, consecutive losses, mode.

---

## Files changed

| File | Change |
|------|--------|
| `bot_state.py` | Add `_s1_window_fired: float = 0.0` + add to `__all__` |
| `bot_strategy.py` | Add window gate in `strategy_brain_s1` after per-asset cap |
| `bot_risk.py` | Set `_s1_window_fired` after fill in `_execute_s1_trade` |
| `bot_loops.py` | S1+S2 dedup in `handle_ready_phase`; `_prev_quiet` tracking in both loops |
| `tests/test_s1_window_guard.py` | New: 5 tests for window guard |
| `tests/test_risk_dedup.py` | Append: 1 test for S1+S2 dedup |
| `tests/test_eod_summary.py` | New: 3 tests for quiet-hours transition detection |

---

## Testing

- **Window guard:** patch `_s1_window_fired` to `time.time() - 30` -> expect skip; patch to `time.time() - 400` -> expect pass-through; ETH cooldown must not block SOL.
- **S1+S2 dedup:** inspect `handle_ready_phase` source for `s2_dedup` string; verify `_s1_pending_trades` checked after `_execute_s1_trade`.
- **EOD summary:** mock `_is_quiet_hours` to flip False->True mid-loop; assert `_check_daily_stats` called exactly once; assert second flip (True->False->True) fires again on next session.
