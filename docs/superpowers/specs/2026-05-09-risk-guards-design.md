# Risk Guards Design — 2026-05-09

## Summary

Four targeted execution/risk fixes for the dual-brain bot. Signal quality issues (win rate smoothing, S2 vel_lookback) deferred pending fresh calibration data.

## Scope

**In scope (this spec):**
1. S2 OBI fail-open → fail-closed
2. S1 global + per-asset position cap
3. S1 consecutive-loss persistence + alert trigger
4. S1 session gate configurable for alts via per-asset config

**Out of scope (deferred):**
- Win rate table smoothing / Laplace correction (requires re-running `calibrate_winrates.py`)
- S2 `vel_lookback` increase for SOL/DOGE (needs backtest validation)
- S1 tanh ceiling adjustment

---

## Fix 1: S2 OBI Fail-Closed

**File:** `bot_strategy.py` — `_s2_obi_gate()`

**Problem:** When `_ticker_obi[ticker]` is absent (fetch failed, not yet populated), gate returns `True, None` — S2 enters on velocity signal alone with no OBI confirmation.

**Fix:** Return `False, None` when `obi_val is None`. S2 skips the market until OBI data is available (next fetch cycle, ~10s).

```python
def _s2_obi_gate(ticker: str, side: str, min_obi: float):
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return False, None   # was: return True, None
    if side == "yes" and obi_val <= min_obi:
        return False, obi_val
    if side == "no"  and obi_val >= -min_obi:
        return False, obi_val
    return True, obi_val
```

**Skip reason logged:** `s2_obi_gate:obi=None` (already handled by existing skip logging in `strategy_brain_s2`).

**Risk:** Permanent OBI fetch failure silences S2 entirely. Existing error logging in `fetch_orderbook` is the signal — no new handling needed here.

---

## Fix 2: S1 Global + Per-Asset Position Cap

**File:** `bot_strategy.py` — `strategy_brain_s1()`

**Problem:** `_s1_pending_trades` has no maximum. Multiple markets per asset can both fire S1. With `_S1_ASSET_VOL_RATIO["DOGE"]=2.6`, two stacked DOGE positions = 5.2× base contracts.

**Config keys (add to `config.json` defaults):**
- `max_s1_positions` — global cap on `len(_s1_pending_trades)`. Default: `3`
- `max_s1_positions_per_asset` — per-asset cap. Default: `1`

**Logic added early in `strategy_brain_s1`, before any signal computation:**

```python
_s1_global_cap = config.get("max_s1_positions", 3)
if len(bot_state._s1_pending_trades) >= _s1_global_cap:
    return _make_skip("yes", "s1_cap_global", abs_pct, mins_left, variant="strategy1")

_s1_asset_cap = config.get("max_s1_positions_per_asset", 1)
_s1_asset_count = sum(
    1 for t in bot_state._s1_pending_trades.values() if t.get("asset") == asset
)
if _s1_asset_count >= _s1_asset_cap:
    return _make_skip("yes", "s1_cap_asset", abs_pct, mins_left, variant="strategy1")
```

**Default `max_s1_positions_per_asset=1`** prevents double-asset stacking entirely. Operator can set to 2 in config if desired.

---

## Fix 3: S1 Consecutive-Loss Persistence + Alert

**Problem:** `_s1_consecutive_losses` is incremented in `_settle_s1_trade` but:
- Never written to state file (resets to 0 on restart)
- Never triggers Telegram alert or pause

Three sub-changes:

### 3a. Persist to state file

**File:** `bot_risk.py` — `write_state_file()` state dict

Add alongside existing `consecutive_losses` key:
```python
"s1_consecutive_losses": bot_state._s1_consecutive_losses,
```

### 3b. Restore on startup

**File:** `bot_loops.py` — startup recovery block (near line 963)

Add after existing `_s2_consecutive_losses` restore:
```python
saved_cl_s1 = _saved.get("s1_consecutive_losses", 0)
if isinstance(saved_cl_s1, int) and saved_cl_s1 > 0:
    bot_state._s1_consecutive_losses = saved_cl_s1
```

### 3c. Telegram alert on threshold

**File:** `bot_risk.py` — `_settle_s1_trade()`, after incrementing `_s1_consecutive_losses`

```python
if outcome == "win":
    bot_state._s1_consecutive_losses = 0
else:
    bot_state._s1_consecutive_losses += 1
    max_cl = config.get("max_consecutive_losses", 5)
    if bot_state._s1_consecutive_losses >= max_cl:
        await send_telegram(
            f"<b>🔵 [S1] {bot_state._s1_consecutive_losses} consecutive losses</b>"
        )
```

No pause/limit trigger — informational only, matching S2 behavior in `handle_locked_phase`.

---

## Fix 4: S1 Session Gate Configurable for Alts

**File:** `bot_strategy.py` — `strategy_brain_s1()`

**Problem:** `session_gate` is hardcoded per-asset in `_S1_ASSET_CONFIG`. Only BTC has `session_gate=True`. No way to enable gating for alts without a code deploy.

**Fix:** Replace the hardcoded `cfg["session_gate"]` check with a per-asset config lookup:

```python
# Before:
if cfg["session_gate"] and not _s1_is_us_session():
    return _make_skip(...)

# After:
_effective_gate = get_asset_config(config, asset, "s1_session_gate", cfg["session_gate"])
if _effective_gate and not _s1_is_us_session():
    return _make_skip("yes", "s1_session_gate", abs_pct, mins_left, variant="strategy1")
```

Operator enables ETH gating with: `"asset_config": {"ETH": {"s1_session_gate": true}}` in `config.json`. No code deploy needed. BTC behavior unchanged (still gated by hardcoded `_S1_ASSET_CONFIG` default).

---

## Files Changed

| File | Change |
|------|--------|
| `bot_strategy.py` | Fix 1: `_s2_obi_gate` fail-closed; Fix 2: S1 cap gates; Fix 4: effective session gate |
| `bot_risk.py` | Fix 3a: persist `s1_consecutive_losses`; Fix 3c: alert in `_settle_s1_trade` |
| `bot_loops.py` | Fix 3b: restore `_s1_consecutive_losses` on startup |
| `config.json` | Add `max_s1_positions: 3`, `max_s1_positions_per_asset: 1` |
| `tests/test_risk_guards.py` | New test file (see Tests section) |

---

## Tests

New file: `tests/test_risk_guards.py`

1. `test_s2_obi_gate_fails_closed_on_none` — `_s2_obi_gate(ticker, "yes", 0.20)` with `_ticker_obi` empty → returns `(False, None)`
2. `test_s2_obi_gate_passes_with_data` — `_ticker_obi[ticker] = 0.40` → returns `(True, 0.40)` for `side="yes"`, `min_obi=0.20`
3. `test_s1_cap_global_source_check` — `inspect.getsource(strategy_brain_s1)` contains `s1_cap_global`
4. `test_s1_cap_asset_source_check` — source contains `s1_cap_asset`
5. `test_s1_consecutive_loss_persisted` — `write_state_file` state dict includes `s1_consecutive_losses` key
6. `test_s1_consecutive_loss_restored` — startup block reads `s1_consecutive_losses` from saved state
7. `test_s1_session_gate_source_check` — `strategy_brain_s1` source contains `get_asset_config` and `s1_session_gate` and `_effective_gate`

---

## Deferred Work

Signal quality hardening (separate spec/plan when calibration data available):
- Laplace smoothing on `_S1_WIN_RATE` 1.0 buckets (minimum sample count floor in `calibrate_winrates.py`)
- S2 `vel_lookback` minimum 4 for all assets (SOL/DOGE currently 3)
- S1 tanh ceiling review for far-OTM buckets
