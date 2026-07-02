# S2 Params Fix + EV Improvement + Dashboard Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix remaining S2 strategy bugs found by council review, raise EV thresholds to stop marginal losing trades, move the trade-amount input to the Settings panel so users can find it, and add a Telegram test button.

**Architecture:** Three strategy code changes (S2 min_dist + time_gate + both-strategy min_ev raise) handled in `bot_strategy.py`. Two dashboard changes handled in `dashboard.html` (move trade amount input, add Telegram test). One server change in `server.py` (Telegram test endpoint). All changes tested with TDD.

**Tech Stack:** Python 3.11, pytest. HTML/JS (no build step — dashboard.html is served directly). Flask (server.py). Kalshi trading bot on Railway.

---

## Background: What the council investigation found

**Bug 1 (Critical): `_S2_ASSET_CONFIG` min_dist=0.001 for all assets doesn't match calibration.**

`scripts/calibrate_winrates.py` S2_ASSET_CONFIG uses per-asset values:
```python
S2_ASSET_CONFIG = {
    "BTC":  dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4),
    "ETH":  dict(min_dist=0.0030, min_vel_delta=0.70, vel_lookback=4),
    "SOL":  dict(min_dist=0.0060, min_vel_delta=1.20, vel_lookback=3),
    "XRP":  dict(min_dist=0.0050, min_vel_delta=0.90, vel_lookback=4),
    "DOGE": dict(min_dist=0.0100, min_vel_delta=1.50, vel_lookback=3),
}
```

But live bot has `min_dist=0.001` for all assets. Live bot fires on close-to-strike trades that were never in calibration data — their actual win rates are unknown and likely lower than the table values.

**Bug 2 (Moderate): S2 time gate doesn't match calibration range.**

Calibration uses `S2_ENTRY_OFFSETS = [2, 4, 6, 8, 10, 12]` minutes remaining. Live bot time gate is `time_min=0.5, time_max=14.0`. For entries outside [2, 12] minutes, the bot falls back to tanh estimation which overestimates WR at late-window entries (calibration shows ETH WR=0.63 in bucket 2 while tanh estimates 0.73). Fix: tighten to `time_min=2.0, time_max=12.5`.

**Bug 3 (Strategy profitability): min_ev=0.01 allows too many marginal trades.**

At 0.01 EV threshold, trades with barely any edge fire. Example: 80% WR at 75c entry gives EV=0.04 — this fires and barely covers fees. Raise to 0.05 for both S1 and S2 to filter out borderline trades. This keeps all high-WR calibrated trades (97% WR at 75c gives EV=0.22) while cutting the long tail of uncertain ones.

**UX Bug: Trade amount input is in the Trades tab, not Settings.** Users looking for the $ amount control go to Settings and can't find it. Fix: add it to the Settings panel with the other bot controls.

**UX Bug: No way to test if Telegram is working.** Add a test button in Settings.

---

## File Map

| File | Change |
|------|--------|
| `bot_strategy.py` | `_S2_ASSET_CONFIG`: fix `min_dist` per-asset; fix `time_min`/`time_max`; raise `min_ev` from 0.01→0.05 for S2. `_S1_ASSET_CONFIG`: raise `min_ev` from 0.01→0.05 for S1. |
| `server.py` | Add `POST /api/test-telegram` endpoint |
| `dashboard.html` | Add trade amount input to Settings panel; add Telegram test button |
| `tests/test_s2_params_calibration.py` | **Create** — verify S2 min_dist matches calibration, time gate in calibrated range, EV gate raises correctly |

---

## Task 1: Fix S2 `_S2_ASSET_CONFIG` min_dist, time gates, and min_ev

**Files:**
- Modify: `bot_strategy.py` (lines ~294-316 for `_S2_ASSET_CONFIG`)
- Test: `tests/test_s2_params_calibration.py`

The calibration `S2_ASSET_CONFIG` is at `scripts/calibrate_winrates.py` lines 66-72 and is the authoritative source for all S2 params.

- [ ] **Step 1: Write failing tests**

Create `tests/test_s2_params_calibration.py`:

```python
"""Tests that live S2 strategy params match calibration script constants.
Blocks param drift between bot_strategy.py and calibrate_winrates.py.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _S2_ASSET_CONFIG

# Calibration constants from scripts/calibrate_winrates.py lines 66-72
_CAL_S2 = {
    "BTC":  dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4),
    "ETH":  dict(min_dist=0.0030, min_vel_delta=0.70, vel_lookback=4),
    "SOL":  dict(min_dist=0.0060, min_vel_delta=1.20, vel_lookback=3),
    "XRP":  dict(min_dist=0.0050, min_vel_delta=0.90, vel_lookback=4),
    "DOGE": dict(min_dist=0.0100, min_vel_delta=1.50, vel_lookback=3),
}

def test_s2_min_dist_matches_calibration():
    """S2 min_dist must match calibration per-asset — win rate tables calibrated on these."""
    for asset, cal in _CAL_S2.items():
        live = _S2_ASSET_CONFIG[asset]["min_dist"]
        assert live == cal["min_dist"], (
            f"S2 {asset} min_dist mismatch: live={live} calibration={cal['min_dist']}. "
            "Win rate tables only cover trades ≥ calibration's min_dist threshold."
        )

def test_s2_time_min_in_calibrated_range():
    """S2 time_min must be ≥ 2.0 (calibration's earliest entry offset)."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["time_min"] >= 2.0, (
            f"S2 {asset} time_min={cfg['time_min']} < 2.0. Calibration only covers "
            "entries at ≥2 min remaining — earlier entries use tanh fallback with wrong WR."
        )

def test_s2_time_max_in_calibrated_range():
    """S2 time_max must be ≤ 12.5 (calibration's latest entry offset + small buffer)."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["time_max"] <= 12.5, (
            f"S2 {asset} time_max={cfg['time_max']} > 12.5. Calibration only covers "
            "entries at ≤12 min remaining."
        )

def test_s2_min_ev_above_marginal_threshold():
    """S2 min_ev must be ≥ 0.05 to filter out borderline trades."""
    for asset, cfg in _S2_ASSET_CONFIG.items():
        assert cfg["min_ev"] >= 0.05, (
            f"S2 {asset} min_ev={cfg['min_ev']} < 0.05. Marginal trades with barely-positive EV "
            "get filtered by fees and slippage in practice."
        )

def test_s1_min_ev_above_marginal_threshold():
    """S1 min_ev must be ≥ 0.05 to filter out borderline trades."""
    from bot_strategy import _S1_ASSET_CONFIG
    for asset, cfg in _S1_ASSET_CONFIG.items():
        assert cfg["min_ev"] >= 0.05, (
            f"S1 {asset} min_ev={cfg['min_ev']} < 0.05."
        )
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_s2_params_calibration.py -v
```

Expected: 5 FAIL — `test_s2_min_dist_matches_calibration`, `test_s2_time_min_in_calibrated_range`, `test_s2_time_max_in_calibrated_range`, `test_s2_min_ev_above_marginal_threshold`, `test_s1_min_ev_above_marginal_threshold`.

- [ ] **Step 3: Fix `_S2_ASSET_CONFIG` in `bot_strategy.py`**

Find `_S2_ASSET_CONFIG` (around line 294). Replace the entire dict:

```python
_S2_ASSET_CONFIG: dict = {
    #           min_dist  min_obi  min_vel_delta  vel_lookback  min_ev  t_min  t_max
    # ALL values MUST match scripts/calibrate_winrates.py S2_ASSET_CONFIG.
    # min_dist and min_vel_delta are calibration inputs; tables are invalid otherwise.
    # time_min/time_max must stay within S2_ENTRY_OFFSETS=[2..12] min range.
    "BTC":  dict(min_dist=0.0035, min_obi=0.02, min_vel_delta=0.80, vel_lookback=4, min_ev=0.05, time_min=2.0, time_max=12.5),
    "ETH":  dict(min_dist=0.0030, min_obi=0.02, min_vel_delta=0.70, vel_lookback=4, min_ev=0.05, time_min=2.0, time_max=12.5),
    "SOL":  dict(min_dist=0.0060, min_obi=0.02, min_vel_delta=1.20, vel_lookback=3, min_ev=0.05, time_min=2.0, time_max=12.5),
    "XRP":  dict(min_dist=0.0050, min_obi=0.02, min_vel_delta=0.90, vel_lookback=4, min_ev=0.05, time_min=2.0, time_max=12.5),
    "DOGE": dict(min_dist=0.0100, min_obi=0.02, min_vel_delta=1.50, vel_lookback=3, min_ev=0.05, time_min=2.0, time_max=12.5),
}
```

- [ ] **Step 4: Raise S1 min_ev in `_S1_ASSET_CONFIG`**

Find `_S1_ASSET_CONFIG` (around line 93). Change `min_ev=0.01` to `min_ev=0.05` for all 5 assets. Only the `min_ev` field changes; all other values (ema_short, ema_long, min_dist, etc.) stay exactly as-is.

Current (example for ETH):
```python
"ETH":  dict(min_dist=0.0030, max_rv=1.0, ema_short=3, ema_long=10, session_gate=False, min_ev=0.01, time_min=0.5, time_max=14.0),
```

New (min_ev only):
```python
"ETH":  dict(min_dist=0.0030, max_rv=1.0, ema_short=3, ema_long=10, session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
```

Do this for all 5 assets: BTC, ETH, SOL, XRP, DOGE.

- [ ] **Step 5: Run the calibration tests**

```
python -m pytest tests/test_s2_params_calibration.py -v
```

Expected: 5 PASS.

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -q --tb=short
```

Expected: 354 passed (349 existing + 5 new), 0 failures.

Note: `tests/test_strategy_params.py` already tests S1 param alignment. If any S1 tests break, verify you only changed `min_ev` and nothing else in `_S1_ASSET_CONFIG`.

Also verify that `tests/test_s2_fires.py` still passes — those tests use 4-minute windows (secs_left=240) which is ≥ 2 min (time_min=2.0), so they should still fire. The new min_dist for ETH is 0.003; the test uses `btc_price=2850, strike=2800 → abs_pct=0.0179 > 0.003` so dist gate still passes.

- [ ] **Step 7: Commit**

```bash
git add bot_strategy.py tests/test_s2_params_calibration.py
git commit -m "fix(strategy): S2 min_dist+time_gate match calibration; raise S1+S2 min_ev to 0.05"
```

---

## Task 2: Add Telegram test endpoint to server.py

**Files:**
- Modify: `server.py`

Adds `POST /api/test-telegram` that sends a test message and returns success/failure. No request body needed.

- [ ] **Step 1: Add endpoint to `server.py`**

Find the `@app.route("/api/reset_pnl")` route (around line 383). Insert this block immediately BEFORE it:

```python
@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    """Send a test Telegram message to verify notification config."""
    import time as _time
    now_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    try:
        notify.send_alert("INFO", f"🔔 <b>Telegram test</b> — {now_str}\nBot notifications are working.")
        return jsonify({"ok": True, "message": "Test message sent"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
```

- [ ] **Step 2: Verify the endpoint is reachable**

```bash
python -c "
import server
client = server.app.test_client()
r = client.post('/api/test-telegram')
print(r.status_code, r.get_json())
"
```

Expected: `200 {'message': 'Test message sent', 'ok': True}` (and a Telegram message in the chat if env vars are set).

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat(server): add POST /api/test-telegram endpoint for notification verification"
```

---

## Task 3: Dashboard — add trade amount + Telegram test to Settings panel

**Files:**
- Modify: `dashboard.html`

Two changes:
1. Add a "$ per trade" row to the Settings panel (Bot Control card) — mirrors the hidden one in the Trades panel.
2. Add a "Test Telegram" button to the Settings panel.
3. Remove the trade amount card from the Trades panel (it's hidden from users there anyway).

- [ ] **Step 1: Read the Settings panel Bot Control card**

The Bot Control card starts at around line 573 (`<div id="panel-settings" class="panel">`). The card ends around line 586 after the mode buttons. Read lines 573-590 to get exact context.

- [ ] **Step 2: Add trade amount and Telegram test to Settings Bot Control card**

Find this block (exact match):
```html
</div>
</div>
</div>
<div class="card" style="padding:16px;margin-bottom:12px;">
<div class="s-section-title">Enabled Markets</div>
```

Replace with:
```html
<div class="s-group">
<label>$ per trade</label>
<div style="display:flex;align-items:center;gap:8px;">
<input type="number" id="s-trade-amount-input" min="1" step="1" value="25"
  style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:13px;font-weight:600;">
<button onclick="saveTradeAmount()" class="s-save">Save</button>
<span id="s-trade-amount-status" style="font-size:11px;color:var(--ink-3);"></span>
</div>
</div>
<div class="s-group">
<label>Telegram</label>
<div style="display:flex;align-items:center;gap:8px;">
<button onclick="testTelegram()" class="s-save">Send Test</button>
<span id="s-telegram-status" style="font-size:11px;color:var(--ink-3);"></span>
</div>
</div>
</div>
</div>
<div class="card" style="padding:16px;margin-bottom:12px;">
<div class="s-section-title">Enabled Markets</div>
```

- [ ] **Step 3: Remove trade amount card from Trades panel**

Find and delete this entire block (the old trade amount card at the top of panel-trades):
```html
<!-- $ per trade -->
<div class="card" style="padding:12px 14px;margin-bottom:12px;display:flex;align-items:center;gap:10px;">
<span style="font-size:12px;font-weight:600;color:var(--ink-2);">$ per trade</span>
<input type="number" id="trade-amount-input" min="1" step="1" value="25" placeholder="25"
style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:13px;font-weight:600;">
<button onclick="setTradeAmount()" style="padding:4px 14px;font-size:12px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;">Set</button>
<span id="trade-amount-status" style="font-size:11px;color:var(--ink-3);"></span>
</div>
```

Note: the button tag on line 523 may be truncated in your editor — read the actual file to get the full line before deleting.

- [ ] **Step 4: Add JS functions**

Find `function setTradeAmount()` in the JavaScript section. Replace it and add the new functions:

```javascript
async function saveTradeAmount() {
    const val = parseInt(document.getElementById('s-trade-amount-input').value);
    const status = document.getElementById('s-trade-amount-status');
    if (!val || val < 1) { status.textContent = 'invalid'; return; }
    try {
        const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({trade_amount_dollars: val})});
        if (!r.ok) throw new Error(r.status);
        status.textContent = 'saved ✓';
    } catch(e) { status.textContent = 'error'; }
    setTimeout(() => { status.textContent = ''; }, 2000);
}

async function testTelegram() {
    const status = document.getElementById('s-telegram-status');
    status.textContent = 'sending…';
    try {
        const r = await fetch('/api/test-telegram', {method:'POST'});
        const d = await r.json();
        status.textContent = d.ok ? 'sent ✓' : `error: ${d.error}`;
    } catch(e) { status.textContent = 'error'; }
    setTimeout(() => { status.textContent = ''; }, 4000);
}

function setTradeAmount() { saveTradeAmount(); }  // keep old name as alias
```

- [ ] **Step 5: Update `initTradeAmountInput` to populate new input**

Find `function initTradeAmountInput()`:
```javascript
function initTradeAmountInput() {
fetch('/api/state').then(r => r.json()).then(d => {
const amt = (d.config || {}).trade_amount_dollars;
if (amt) document.getElementById('trade-amount-input').value = amt;
}).catch(() => {});
}
```

Replace with:
```javascript
function initTradeAmountInput() {
    fetch('/api/state').then(r => r.json()).then(d => {
        const amt = (d.config || {}).trade_amount_dollars;
        if (amt) {
            const oldEl = document.getElementById('trade-amount-input');
            if (oldEl) oldEl.value = amt;
            const newEl = document.getElementById('s-trade-amount-input');
            if (newEl) newEl.value = amt;
        }
    }).catch(() => {});
}
```

- [ ] **Step 6: Run full test suite (no new tests for HTML)**

```
python -m pytest tests/ -q --tb=short
```

Expected: 354 passed, 0 failures (no test change for this task).

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat(dashboard): move trade amount to Settings panel; add Telegram test button"
```

---

## Task 4: Final verify and push

- [ ] **Step 1: Run full suite**

```
python -m pytest tests/ -q
```

Expected: 354 passed, 0 failures.

- [ ] **Step 2: Verify S2 params by running the brain with new params**

```bash
python -c "
import sys, os, time, collections
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
os.chdir('C:/Users/alxnt/kalshi-bot')
from bot_strategy import strategy_brain_s2, _S2_ASSET_CONFIG, _S1_ASSET_CONFIG
import bot_state

# Verify ETH min_dist=0.003 (was 0.001)
print('ETH min_dist:', _S2_ASSET_CONFIG['ETH']['min_dist'])  # expect 0.003
print('ETH time_min:', _S2_ASSET_CONFIG['ETH']['time_min'])  # expect 2.0
print('ETH time_max:', _S2_ASSET_CONFIG['ETH']['time_max'])  # expect 12.5
print('ETH min_ev:',  _S2_ASSET_CONFIG['ETH']['min_ev'])     # expect 0.05
print('S1 ETH min_ev:', _S1_ASSET_CONFIG['ETH']['min_ev'])   # expect 0.05

# Verify S2 now correctly skips close-to-strike trades (abs_pct=0.001 < min_dist=0.003)
ticker = 'KXETH-TEST'
hist = collections.deque(maxlen=60)
for i in range(6):
    hist.append((time.time()-(5-i)*10, 70+i*0.53))
bot_state._contract_price_history[ticker] = hist
# price=2801 (0.04% from strike) should be skipped by dist gate
result = strategy_brain_s2(2801.0, 2800.0, 72.0, 28.0, 760.0, 360.0, ticker, asset='ETH')
print('Close-to-strike skip:', result.get('action'), result.get('reasoning'))
"
```

Expected output:
```
ETH min_dist: 0.003
ETH time_min: 2.0
ETH time_max: 12.5
ETH min_ev: 0.05
S1 ETH min_ev: 0.05
Close-to-strike skip: skip s2_dist_gate:0.0004<0.003
```

- [ ] **Step 3: Push to GitHub**

```bash
git push origin main
```

Railway auto-deploys on push. Monitor Telegram for first S2 trade notification after deploy.

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|---|---|
| S2 min_dist per-asset matches calibration | Task 1 |
| S2 time gate in calibrated range [2.0, 12.5] | Task 1 |
| S1 and S2 min_ev raised to 0.05 | Task 1 |
| Tests verify all four param constraints | Task 1 |
| Telegram test endpoint | Task 2 |
| Trade amount in Settings panel | Task 3 |
| Old trade amount card removed from Trades panel | Task 3 |
| Telegram test button in Settings panel | Task 3 |
| initTradeAmountInput populates new Settings input | Task 3 |
| Full suite 354 pass | Task 4 |

**Placeholder scan:** No TBD, no "similar to task N." All code blocks are complete.

**Type consistency:** `saveTradeAmount()` and old `setTradeAmount()` are consistent — old function kept as alias.

**What NOT changed:**
- S1 ema_short, ema_long, min_dist, max_rv, time_min, time_max — all unchanged
- S2 min_vel_delta, vel_lookback — already fixed last session
- `_S2_WIN_RATE` and `_S1_WIN_RATE` tables — not touched
- OBI gate behavior — not touched
- Any other bot logic — not touched
