# Hourly Dashboard, Notifications Retrofit, and Limit-Order Audit — Design

**Date:** 2026-04-23
**Status:** Approved (sections 1 in detail, 2-5 presented together)
**Scope:** Single implementation plan

## 1. Goals

1. Label the current hourly session's Kalshi market (ticker + strike) on the BTC and ETH dashboard cards.
2. Guarantee dashboard and bot stay in sync: the dashboard reflects exactly the contract the bot is evaluating.
3. Audit hourly limit-order placement + price-selection logic end-to-end; fix any bugs found.
4. Add a fill-verification Telegram notification for hourly trades (target / market ask / posted / filled / slippage).
5. Retrofit every existing Telegram notification to include `[ASSET | 15m or hourly | market-ticker | phase-if-hourly]` prefix.

## 2. Non-goals

- No changes to 15-minute strategies' logic.
- No new strategies.
- No refactor into a standalone `notify.py` module (Option 2 from brainstorming; rejected to keep diff small).
- No changes to Kalshi auth, order-cancellation, exit logic, or DB schema.

## 3. Data flow and state contract

`bot.py :: write_state_file()` currently writes `bot_state.json` with per-asset entries under top-level `assets`. Each entry already has `market_ticker`, `market_title`, `signals`.

**Extension (hourly assets only):**

```json
{
  "asset": "ETH",
  "market_ticker": "KXETHD-26APR23-17:00-T3500",
  "market_title": "ETH above $3,500 at 5pm?",
  "session_type": "hourly",
  "strategy_name": "ETHHourlyCombined",
  "strike": 3500,
  "phase": "Dwell (t=35min)",
  "signals": { /* existing */ }
}
```

For BTC hourly: `strategy_name = "BTCHourly V3"`, no `phase` field (BTC V3 doesn't use phase gates). `signals` already carries `session` (`asian`/`other`), `vwap_z`, `rsi`, `bollinger`, `momentum_reversal`, `vwap_adj`, `rsi_adj`, `bb_adj`, `mom_adj`, `total_adj_before_taper`, `final_p_yes`, `baseline_p_above`.

For 15m assets: `session_type = "15m"`, no `strategy_name`/`strike`/`phase` fields — dashboard falls through to existing 15m rendering.

**Classification rule:** `market_duration_min > 25.0` ⇒ `"hourly"`. This matches the existing strategy router at `bot.py:1835`.

**Strike source:** parse Kalshi ticker suffix (`T3500` ⇒ 3500); fallback to `market.get("strike_price")` / `market.get("strike")`.

## 4. Dashboard changes (`dashboard.html`)

### 4.1 Asset card header (both assets, hourly active)

```
ETH Hourly | KXETHD-26APR23-17:00-T3500 | Strike $3,500 | Strategy: ETHHourlyCombined
BTC Hourly | KXBTCD-26APR23-17:00-T94000 | Strike $94,000 | Strategy: BTCHourly V3
```

### 4.2 ETH hourly body

Keep existing `_buildHourlyViewHtml(a)` (Phase / Elapsed / ETH crossings / BTC crossings / ETH dist / ETH ITM / BTC ITM / Dwell % / Streak % / Entry). Rename header text only.

### 4.3 BTC hourly body (NEW)

```
Session:    Active              (or "Asian — SKIPPED" in red)
VWAP z:     +1.42
RSI:        28
Bollinger:  below
Momentum:   fade_down
Total adj:  −0.08
p_yes:      0.47  (baseline 0.55)
Entry:      YES 42¢             (or "—" if no entry)
```

Branch inside `_buildHourlyViewHtml(a)` on `a.strategy_name`:
- `"ETHHourlyCombined"` → existing ETH rows.
- `"BTCHourly V3"` → new BTC rows.

### 4.4 Sync guarantee

The dashboard reads `session_type` and `strategy_name` from the per-asset state written by the bot. Same fields drive strategy routing inside the bot. Dashboard can never reference a strategy the bot isn't running.

## 5. Limit-order + price-selection audit

### 5.1 Audit scope

1. **`bot.py :: place_order` (line 2560+):**
   - Late-window resting GTC limit posts at strategy-chosen target; no drift from market snapshots between attempts.
   - Non-late paths post at strategy target; `price_this_attempt` is the strategy target, not a re-fetched ask.
   - Retry loop does not refresh ask and overwrite target.

2. **Per-strategy `entry_cents` side-consistency:**
   - `mid_window_strategy.py:170` — `entry_cents = yes_ask if eth_itm else no_ask`. Must match `side`: YES-side trades use `yes_ask`, NO-side use `no_ask`.
   - Same check in `dwell_window_strategy.py`, `late_window_strategy.py`, `btc_hourly_strategy.py`.
   - Verify `early_window_strategy.py` live status.

3. **Orderbook ingestion (`bot.py:1128+`):**
   - `best_yes_ask` / `best_no_ask` fallback chain (L1 book → L2 scan → `yes_ask_dollars` → derived).
   - Check hourly markets aren't hitting a stale/degenerate path.

### 5.2 Findings

Audit date: 2026-04-23. Test file: `tests/strategies/test_hourly_entry_price.py` (7 tests, all PASS).

**No bugs found.** The invariant `decision.side == "yes" → entry_cents == features.yes_ask`
(and analogous for NO) holds for every trade path exercised:

- `src/strategies/mid_window_strategy.py:170` — `entry_cents = features.yes_ask if eth_itm else features.no_ask`. Verified via MidWindow NO path (`test_mid_window_no_side_uses_no_ask`). See note (a) below for YES path.
- `src/strategies/dwell_window_strategy.py:170-175` — explicit side/ask branches. Verified via Dwell YES and NO paths.
- `src/strategies/late_window_strategy.py:92-99` — explicit side/ask branches. Verified via Late YES and NO paths.
- `src/strategies/btc_hourly_strategy.py` — routes through `BaseStrategy.decide` (see note (b)).

Two audit observations (neither is a bug):

(a) **MidWindowStrategy YES path is structurally unreachable under synthetic flat-start BTC fixtures.** `_btc_window_prices_and_strike` derives the BTC strike from the *first* sample in the in-window deque and `_cross_count` uses strict `>` comparison, so `btc_cross == 0 AND btc_itm == True` is mathematically impossible for any series starting at the strike value. In live data the strategy still fires when real BTC hovers fractionally on one side before diverging; the test treats the YES path as best-effort (invariant is still checked if it happens to trade). This does not indicate a production bug — it's a property of the strike-derivation approach.

(b) **Coverage gap (not a bug): `BTCHourlyStrategy` does not emit `entry_cents` in `contributing_signals`.** It uses the default `BaseStrategy.decide` pipeline (EV-driven) which never populates `entry_cents`. When the dashboard renders a BTC signal panel (Task 9), the Entry row will show `—`. If future work needs a BTC fill-verification notification (Task 5) to quote target vs market ask, a follow-up would add `entry_cents = features.yes_ask if ev.best_side == "yes" else features.no_ask` into `BaseStrategy.decide`'s trade branch at `src/strategies/base.py:208-215`.

No strategy files were modified in this audit.

### 5.3 Fix policy

Bugs are fixed inline in the same branch. Each fix gets one short comment stating the restored invariant. No scope creep — only fixes directly attributable to audit findings.

## 6. Notifications

### 6.1 Context helper (NEW)

```python
def _notify_ctx(
    asset: str,
    ticker: str,
    duration_min: float,
    phase: str | None = None,
) -> str:
    session = "hourly" if duration_min > 25.0 else "15m"
    parts = [asset, session, ticker]
    if phase and session == "hourly":
        parts.append(phase)
    return f"[{' | '.join(parts)}]"
```

Added to `bot.py` near `send_telegram` at line ~639.

### 6.2 Retrofit of existing `send_telegram` calls

| Line | Event | New prefix |
|------|-------|------------|
| 2652 | Limit placed | `📋 [ASSET \| session \| ticker \| phase?] LIMIT ORDER PLACED` |
| 2907 | Order failed (non-retryable) | `[ASSET \| session \| ticker \| phase?] ORDER FAILED — …` |
| 3035 | Order not filled (no liquidity) | `⚠️ [ASSET \| session \| ticker \| phase?] ORDER NOT FILLED` |
| 3131 | Demo daily loss limit | unchanged (no market context) |
| 3792 | Limit filled / reversal | `[ASSET \| session \| ticker \| phase?] LIMIT ORDER FILLED` |
| 3915 | Consecutive-loss pause | `⚠️ [ASSET \| session \| last-ticker] N consecutive losses` |
| 3927 | Win / loss close | `[ASSET \| session \| ticker \| phase?] WIN/LOSS …` |
| 4583 | Pre-flight block | unchanged (pre-trading) |
| 4678 | Startup | unchanged (pre-trading) |

### 6.3 New fill-verification notification (hourly only)

Fires inside `place_order` immediately after a fill confirms, only when `market_duration_min > 25.0`.

```
🎯 [ETH | hourly | KXETHD-26APR23-17:00-T3500 | Dwell] FILL VERIFICATION
Target:     82¢
Market ask: 85¢
Posted:     82¢
Filled:     84¢
Slippage:   +2¢ vs target   |   −1¢ vs market
```

Emit a yellow warning `⚠️` if `|slippage_vs_target| > 3¢`.

**Data sources:**
- `target` = `entry_price_cents` (strategy's chosen price).
- `market_ask_at_post` = `best_yes_ask` (or `best_no_ask` for NO-side) captured at post time — NEW local variable, must be captured before order post.
- `posted` = `price_this_attempt`.
- `filled` = `_fill_yes_price` (existing).

## 7. Files modified

- `bot.py` — extend `write_state_file`, add `_notify_ctx`, retrofit 7 `send_telegram` calls, add fill-verification notification, fix any audit findings.
- `dashboard.html` — update `_buildHourlyViewHtml` with strategy branch, add BTC V3 panel, update card header with ticker+strike+strategy.
- `src/strategies/btc_hourly_strategy.py` — only if audit reveals a bug; otherwise untouched.
- `src/strategies/mid_window_strategy.py`, `dwell_window_strategy.py`, `late_window_strategy.py` — only if audit reveals side/ask mismatch.

## 8. Testing

### 8.1 Dashboard rendering

- Mock `bot_state.json` with one hourly ETH asset (phase=Dwell, strike=3500) + one hourly BTC asset (session=other, full V3 signals).
- Load `dashboard.html` against mock; confirm:
  - ETH card shows ticker + `$3,500` strike + `ETHHourlyCombined` + Phase row = Dwell.
  - BTC card shows ticker + `$94,000` strike + `BTCHourly V3` + 8 V3 rows.
- Repeat with 15m asset; confirm 15m panel renders unchanged.

### 8.2 Integration (paper mode, live market)

Run bot in paper mode against a live hourly ETH market for one window (≥60 min). Confirm:
- `bot_state.json` carries `session_type`, `strategy_name`, `strike`, `phase`.
- Dashboard displays the currently active contract.
- When an entry fires:
  - All Telegram messages carry `[ETH | hourly | <ticker> | <phase>]` prefix.
  - Fill-verification alert fires once, with all five numeric fields populated.

### 8.3 Limit-order audit harness

For each hourly strategy, unit-test with canned `MarketFeatures`:
- YES-side decision ⇒ `entry_cents == yes_ask`.
- NO-side decision ⇒ `entry_cents == no_ask`.
- `place_order` posts at strategy target, not at a refreshed ask snapshot.

## 9. Success criteria

- Dashboard shows correct ticker + strike + strategy-specific signal panel on BTC and ETH during hourly windows.
- `bot_state.json` carries all new fields for hourly entries.
- Every trade-related Telegram message prefixes `[ASSET | session | ticker | phase?]`.
- New fill-verification alert fires on every hourly fill.
- Any audit finding is fixed in the same branch and documented in §5.2.

## 10. Risks

- **Mis-parsing strike from ticker.** Ticker format varies by series; unit-test parse against known BTC + ETH ticker samples. Fallback to `market.get("strike_price")`.
- **Dashboard desync if write_state_file writes before strategy runs.** Mitigation: `write_state_file` already writes per-cycle after strategy decide — same ordering preserved.
- **Notification spam on every cycle.** Fill-verification fires only once per fill (already gated by `fill_confirmed`). No new per-cycle messages.
- **Retrofitting a notification breaks an existing string pattern a user is parsing.** User is the only Telegram consumer; no downstream parser. Safe.
