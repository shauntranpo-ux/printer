# Go-Live Reliability Audit - Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix every bug that could silently lose real money or leave positions untracked before switching the bot from paper to live mode. No strategy-logic changes - reliability and correctness only.

**Constraint:** All 606 existing tests must pass before and after every task. `py -3 -c "import bot"` must succeed after every task.

---

## Context

The bot has two concurrent loops: `main_loop` (BTC via S2 D3 hybrid) and `_non_btc_asset_loop` (ETH, SOL, XRP, DOGE via `_process_asset`). Both use `handle_ready_phase` / `handle_locked_phase` from `bot_loops.py`. S1 runs alongside S2 inside the BTC loop via `_execute_s1_trade` in `bot_risk.py`.

Key files:
- `bot_loops.py` - phase handlers, main loop, non-BTC loop
- `bot_market.py` - `place_order`, `_verify_order_fill`, `_portfolio_has_position`
- `bot_risk.py` - `check_daily_limits`, `_execute_s1_trade`, `_settle_s1_trade`, `_try_settle_orphaned_s1`
- `bot_strategy.py` - `strategy_brain_s1`, `strategy_brain_s2`, `_session_ev_adjustment`
- `bot_state.py` - all shared globals

---

## Bug Inventory (priority order)

### Bug 1 - Untracked live position on fill-verify failure (`bot_market.py:813`)

**What happens:**
`_verify_order_fill` returns `False` on any network exception (line 813-814: "returning False (conservative)"). Back in `handle_ready_phase` (line 388-396), `fill_confirmed=False` sets `phase=DONE` and returns - without writing a DB trade record. But the HTTP order request already reached Kalshi. In live mode this means real contracts are open with no record and no settlement logic.

**Fix:**
Before setting `phase=DONE` on `fill_confirmed=False` in live/demo mode, make one final attempt via `_portfolio_has_position(session, ticker, side)`. If that returns True, treat it as a confirmed fill (use `entry_price_cents` as the fill price, write the DB record, set phase=LOCKED). Only fall through to DONE if the portfolio check also returns False.

Paper mode is unaffected (paper fill is always confirmed).

---

### Bug 2 - S1 orphan positions not settled on restart (`bot_loops.py:869`)

**What happens:**
On startup, `main_loop` queries the DB for S1 trades with `outcome='pending'` and logs a warning per orphan (lines 869-884). It says "manual reconciliation may be needed." Nothing is auto-settled. Those contracts are live on Kalshi and will resolve, but the bot's DB never records the outcome or P&L.

**Fix:**
After the orphan warning loop, for each orphan row: fetch the market result from Kalshi (`GET /markets/{market_id}`), and if result is `"yes"` or `"no"`, call `_settle_s1_trade(ticker, market_result, btc_price, config, asset)`. If the market is still open (`result` is None/null), add the ticker back to `bot_state._s1_pending_trades` so the running loop can settle it normally when it resolves. A `session` is needed for this - do it after the `aiohttp.ClientSession` is created (line 889), not before.

---

### Bug 3 - Non-BTC LOCKED positions lost on restart (`bot_loops.py:802`)

**What happens:**
BTC positions are recovered from `_STATE_FILE` at startup (lines 846-864). Non-BTC asset state lives in `bot_state._asset_states` (in-memory dict). A crash while a non-BTC asset is LOCKED means the position is gone from the bot's view on restart - the asset restarts in WATCH and will try to trade again next cycle. The open position on Kalshi is never settled by the bot.

**Fix:**
Persist per-asset state for non-BTC assets. Extend `write_state_file` in `bot_risk.py` to write a `"asset_positions"` key to `_STATE_FILE` containing `{asset: {"phase": ..., "position": ...}}` for any non-BTC asset currently in LOCKED phase. On `main_loop` startup (after `_STATE_FILE` is read), restore these into `bot_state._asset_states` so `_process_asset` sees the correct LOCKED state and routes to `handle_locked_phase`.

---

### Bug 4 - `_session_ev_adjustment` dead import (`bot_strategy.py:37`, `bot_loops.py:25`)

**What happens:**
`_session_ev_adjustment()` is defined (returns 0.0) and imported into `bot_loops.py` but never called anywhere. It was intended to adjust the EV floor based on session timing but was never wired in. Leaving it in is misleading - callers would assume the EV floor accounts for session-level adjustments when it doesn't.

**Fix:**
Remove the function from `bot_strategy.py` and its import from `bot_loops.py`. The EV gate is already applied correctly in `strategy_brain_s1` (lines 479-490). If session-level EV adjustment is needed in future, it should be implemented properly then.

---

### Bug 5 - Daily loss limit only enforced in live/demo, skipped in paper (`bot_risk.py:70`)

**What happens:**
`check_daily_limits` returns `(False, "")` immediately when `mode == "paper"` (line 70-71). This is by design - paper mode isn't supposed to enforce limits. But the current config.json has `daily_loss_limit_dollars: 20` and `daily_profit_target_dollars: 50`. When going live, the first check against today's P&L will query only live-mode trades (`db_get_today_pnl(mode)` is mode-aware). This is correct. But the limit state (`bot_state.limit_triggered`) is never reset when switching from paper to live mid-session.

**Fix:**
In `midnight_reset()`, also reset `limit_triggered = False` and `limit_reason = ""`. Additionally, after any config write that changes `mode` (in `server.py` `api_config`), reset `limit_triggered` in bot_state so the new mode starts clean. Document the paper→live limit semantics with a comment in `check_daily_limits`.

---

### Bug 6 - S2 double-trade risk if bot crashes between fill and state write (`bot_loops.py:433-468`)

**What happens:**
In `handle_ready_phase`: after `db_write_trade` (line 433), `bot_state.current_position` and `bot_state.current_phase = "LOCKED"` are set in-memory (lines 463-468). The state file is written at the top of the NEXT loop iteration (~10 seconds later). If the bot crashes in that window, on restart: `_STATE_FILE` still shows the old phase, recovery doesn't trigger, and `_order_attempted_tickers` is cleared (in-memory). The bot could try to trade the same ticker again.

**Fix:**
Write the state file immediately after setting `current_phase = "LOCKED"` in `handle_ready_phase`, before returning. Add a `await write_state_file(...)` call right after line 468. This closes the crash window. Also, on startup, after the S1 orphan check, query the DB for any S2 trades with `outcome='pending'` from the current session date that don't have a matching `_STATE_FILE` recovery - log a warning if found.

---

## Verification Plan

After all bugs are fixed:

1. **Unit tests for fill-verify fallback** - mock `_verify_order_fill` to raise an exception, mock `_portfolio_has_position` to return True → confirm phase=LOCKED and trade is DB-written.
2. **S1 orphan settlement test** - seed DB with a pending S1 trade, call startup recovery logic with a mocked Kalshi response returning `result="yes"` → confirm `db_update_trade` called with `outcome="won"`.
3. **Non-BTC state persistence test** - write a LOCKED asset state, simulate restart by re-running the startup recovery block, confirm `_asset_states` contains the LOCKED position.
4. **`_session_ev_adjustment` removed** - grep confirms no reference in any `.py` file.
5. **Daily limit reset test** - set `limit_triggered=True`, call `midnight_reset()`, confirm `limit_triggered=False`.
6. **State file write-on-LOCKED test** - confirm `write_state_file` is called immediately after phase=LOCKED transition.
7. **Full 606-test suite passes.**

---

## Out of Scope (Part B - separate spec)

- Signal edge analysis: why S1 and S2 are at -$1k, BV3 table accuracy, D3 predictive power
- Strategy parameter tuning
- New features (position scaling, multi-leg, etc.)
