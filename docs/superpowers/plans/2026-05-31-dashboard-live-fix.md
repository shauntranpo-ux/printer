# Dashboard Live Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port all UI improvements (Settings tab, S1/S2 mode toggles, trade amount, Telegram test, EV display fix) from the dead `dashboard.html` to `handoff/Money Printer.html` which is the actual live dashboard served by Railway.

**Architecture:** `server.py` serves `handoff/Money Printer.html` for both `/` and `/new` routes. `dashboard.html` (root) is never served — it's dead code. All previous session improvements went into the wrong file. This plan fixes the live dashboard and removes the dead file.

**Tech Stack:** Vanilla HTML/CSS/JS in `handoff/Money Printer.html`; Flask `server.py`; `/api/config` GET+POST endpoints already support `s1_mode`/`s2_mode`.

---

## File Map

| File | Action | Reason |
|------|--------|--------|
| `handoff/Money Printer.html` | Modify | Add Settings tab, S1/S2 toggles, trade amount input, Telegram test, fix EV display |
| `dashboard.html` | Delete via git rm | Dead file — never served |

---

### Task 1: Fix EV display bug (`*100` double-multiply)

**Files:**
- Modify: `handoff/Money Printer.html:1135`

**Context:** Line 1135 has `(_evVal*100).toFixed(1)+'%'` but `_evVal` (`= m.ev ?? 0`) is already stored as a percent value (e.g. `7.6` not `0.076`). Multiplying by 100 again gives `-1270%` / `+4890%` on cards. Also add ±200 display cap so stale data never shows absurd values.

- [ ] **Step 1: Write failing test**

```python
# tests/test_ev_display_bug.py
def test_ev_already_percent_not_scaled():
    """Ensure ev stored in bot_state is percent-scale (>1), not 0-1 fraction."""
    import json, os
    state_path = os.path.join(os.path.dirname(__file__), '..', 'bot_state.json')
    if not os.path.exists(state_path):
        return  # CI skip
    with open(state_path) as f:
        state = json.load(f)
    for asset_data in state.get('assets', {}).values():
        ev = asset_data.get('eval', {}).get('brain_ev')
        if ev is not None:
            # brain_ev in state is raw 0-1 fraction; ev on market card is brain_ev*100
            assert abs(ev) < 5.0, f"brain_ev={ev} looks like percent, not fraction"
```

- [ ] **Step 2: Run test to verify state**

```bash
pytest tests/test_ev_display_bug.py -v
```

Expected: PASS (confirms brain_ev stored as fraction < 5.0, so `*100` in JS is correct, but `m.ev` at line 1853 already applies `*100` before storage in MARKETS array — see next step).

- [ ] **Step 3: Trace the data path and find the actual bug**

Read `handoff/Money Printer.html` line 1853:
```
ev: a.ev ?? dec.yes_ev ?? ex.ev ?? 0,
```
`a.ev` comes from `/api/market-state` → `bot_risk.py` write_state_file which stores `round(brain_ev * 100, 1)`. So `m.ev` is already percent (e.g. `7.6`). Line 1135 multiplies by 100 again → `760%`.

Fix at line 1135 — change:
```javascript
${_evVal!=0?((_evVal>0?'+':'')+(_evVal*100).toFixed(1)+'%'):'—'}
```
to:
```javascript
${_evVal!=0&&Math.abs(_evVal)<=200?((_evVal>0?'+':'')+_evVal.toFixed(1)+'%'):_evVal!=0?'—*':'—'}
```

- [ ] **Step 4: Apply the fix**

In `handoff/Money Printer.html` find the exact line 1135 string and replace it. Use Read to confirm exact whitespace before editing.

- [ ] **Step 5: Also guard line 1288 (decision signals panel)**

Find: `${evVal>=0?'+':''}${evVal.toFixed(1)}%`
Replace: `${Math.abs(evVal)<=200?(evVal>=0?'+':'')+evVal.toFixed(1)+'%':'—*'}`

This guards the decision signals row EV display.

- [ ] **Step 6: Verify no syntax errors**

```bash
python -c "
import re
with open('handoff/Money Printer.html') as f:
    content = f.read()
# Count template literal backtick balance inside script tag
script = content[content.index('<script>'):content.rindex('</script>')]
print('Script length:', len(script))
print('OK')
"
```
Expected: prints `OK`.

- [ ] **Step 7: Commit**

```bash
git add "handoff/Money Printer.html"
git commit -m "fix(dashboard): remove x100 double-multiply in EV display; add ±200 stale-data guard"
```

---

### Task 2: Add Settings tab + panel to live dashboard

**Files:**
- Modify: `handoff/Money Printer.html` (lines 629, 855, 1670)

**Context:** The live dashboard has tabs: Overview, BTC, ETH, SOL, XRP, DOGE, Trades. Need to add Settings tab with: S1 mode toggle (PAPER/LIVE), S2 mode toggle (PAPER/LIVE), trade amount input, Telegram test button. The global mode selector (PAPER/DEMO/LIVE in topbar) stays as-is — it controls the overall bot mode. S1/S2 modes are per-strategy overrides.

- [ ] **Step 1: Write failing test**

```python
# tests/test_settings_tab.py
def test_settings_tab_exists_in_live_dashboard():
    with open('handoff/Money Printer.html', encoding='utf-8') as f:
        html = f.read()
    assert 'data-tab="settings"' in html, "Settings tab button missing"
    assert 'id="panel-settings"' in html, "Settings panel div missing"
    assert 'setStrategyMode' in html, "setStrategyMode JS function missing"
    assert 's1_mode' in html, "s1_mode missing from Settings panel"
    assert 's2_mode' in html, "s2_mode missing from Settings panel"
    assert 'testTelegram' in html, "testTelegram JS function missing"
    assert 'saveTradeAmount' in html, "saveTradeAmount JS function missing"
    assert 'api/test-telegram' in html, "Telegram test API call missing"

def test_settings_tab_absent_from_dead_dashboard():
    import os
    assert not os.path.exists('dashboard.html'), "dashboard.html should be deleted"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_settings_tab.py -v
```
Expected: FAIL — tab missing, panel missing, functions missing.

- [ ] **Step 3: Add Settings tab button**

In `handoff/Money Printer.html`, find line 629:
```html
      <button class="tab" data-tab="trades">Trades</button>
```
Replace with:
```html
      <button class="tab" data-tab="trades">Trades</button>
      <button class="tab" data-tab="settings">Settings</button>
```

- [ ] **Step 4: Add Settings panel HTML**

Find the closing `</main>` tag (line 857 area):
```html
</main>
```
Replace with:
```html
<div class="panel" id="panel-settings">
  <div style="max-width:480px;margin:0 auto;display:flex;flex-direction:column;gap:16px;padding:16px 0;">

    <div style="background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:20px;">
      <div style="font:600 11px/1 'Montserrat',sans-serif;letter-spacing:0.08em;text-transform:uppercase;color:var(--ink-3);margin-bottom:16px;">Strategy Modes</div>

      <div style="display:flex;flex-direction:column;gap:14px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div>
            <div style="font:600 13px/1 'Montserrat',sans-serif;color:var(--ink);">S1 — EMA Momentum</div>
            <div style="font:400 11px/1 'Montserrat',sans-serif;color:var(--ink-3);margin-top:4px;">3-min vs N-min EMA crossover</div>
          </div>
          <div style="display:flex;gap:4px;align-items:center;">
            <button id="s1-paper-btn" class="mode-seg-btn" onclick="setStrategyMode('s1','paper')">PAPER</button>
            <button id="s1-live-btn"  class="mode-seg-btn" onclick="setStrategyMode('s1','live')">LIVE</button>
            <span id="s1-mode-status" style="font-size:10px;color:var(--ink-3);margin-left:4px;min-width:36px;"></span>
          </div>
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div>
            <div style="font:600 13px/1 'Montserrat',sans-serif;color:var(--ink);">S2 — Velocity + OBI</div>
            <div style="font:400 11px/1 'Montserrat',sans-serif;color:var(--ink-3);margin-top:4px;">Contract velocity + orderbook imbalance</div>
          </div>
          <div style="display:flex;gap:4px;align-items:center;">
            <button id="s2-paper-btn" class="mode-seg-btn" onclick="setStrategyMode('s2','paper')">PAPER</button>
            <button id="s2-live-btn"  class="mode-seg-btn" onclick="setStrategyMode('s2','live')">LIVE</button>
            <span id="s2-mode-status" style="font-size:10px;color:var(--ink-3);margin-left:4px;min-width:36px;"></span>
          </div>
        </div>
      </div>
    </div>

    <div style="background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:20px;">
      <div style="font:600 11px/1 'Montserrat',sans-serif;letter-spacing:0.08em;text-transform:uppercase;color:var(--ink-3);margin-bottom:16px;">Trade Amount</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <span style="font:400 12px/1 'Montserrat',sans-serif;color:var(--ink-2);">$</span>
        <input id="s-trade-amount" type="number" min="1" max="500" step="1"
          style="width:90px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);color:var(--ink);font:600 14px/1 'Montserrat',monospace;outline:none;" />
        <button onclick="saveTradeAmount()"
          style="padding:6px 14px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);color:var(--ink-2);font:600 11px/1 'Montserrat',sans-serif;cursor:pointer;letter-spacing:0.06em;">
          SAVE
        </button>
        <span id="trade-amount-status" style="font-size:10px;color:var(--ink-3);"></span>
      </div>
    </div>

    <div style="background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:20px;">
      <div style="font:600 11px/1 'Montserrat',sans-serif;letter-spacing:0.08em;text-transform:uppercase;color:var(--ink-3);margin-bottom:16px;">Notifications</div>
      <button onclick="testTelegram()"
        style="padding:8px 16px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);color:var(--ink-2);font:600 11px/1 'Montserrat',sans-serif;cursor:pointer;letter-spacing:0.06em;">
        Send Test Message
      </button>
      <span id="telegram-status" style="font-size:10px;color:var(--ink-3);margin-left:8px;"></span>
    </div>

  </div>
</div>

</main>
```

- [ ] **Step 5: Add CSS for mode-seg-btn**

Find the existing `.mode-seg button{` CSS block (around line 110-120 area) and add after it:
```css
.mode-seg-btn{padding:4px 10px;border:1px solid var(--line);border-radius:5px;background:var(--panel-2);color:var(--ink-3);font:600 10px/1 'Montserrat',sans-serif;letter-spacing:0.06em;cursor:pointer;}
.mode-seg-btn.active-paper{background:var(--panel-2);color:var(--ink-2);border-color:var(--line-2);}
.mode-seg-btn.active-live{background:oklch(0.42 0.13 155 / 0.12);color:oklch(0.42 0.13 155);border-color:oklch(0.42 0.13 155 / 0.4);}
```

- [ ] **Step 6: Add settings case to switchTab()**

Find the `switchTab` function closing block (around line 1670):
```javascript
  } else if(MARKET_SYMS.includes(tab)){
    _activeMarketSym = tab;
    document.getElementById('panel-market').classList.add('active');
    renderMarketPanel(tab);
    fetchMarketDetail(tab);
  }
  window.scrollTo(0,0);
}
```
Replace with:
```javascript
  } else if(MARKET_SYMS.includes(tab)){
    _activeMarketSym = tab;
    document.getElementById('panel-market').classList.add('active');
    renderMarketPanel(tab);
    fetchMarketDetail(tab);
  } else if(tab==='settings'){
    _activeMarketSym = null;
    document.getElementById('panel-settings').classList.add('active');
    fetchSettingsConfig();
  }
  window.scrollTo(0,0);
}
```

- [ ] **Step 7: Add JS functions for Settings**

Just before the `document.querySelectorAll('.tab').forEach` block (line 1674 area), add:
```javascript
// ── Settings panel ───────────────────────────────────────────────────────────
async function fetchSettingsConfig(){
  try {
    const d = await fetch('/api/config').then(r=>r.json());
    _applyStrategyMode('s1', d.s1_mode || 'paper');
    _applyStrategyMode('s2', d.s2_mode || 'paper');
    const ta = document.getElementById('s-trade-amount');
    if(ta && d.trade_amount != null) ta.value = d.trade_amount;
  } catch(e){}
}

function _applyStrategyMode(strat, mode){
  ['paper','live'].forEach(m=>{
    const btn = document.getElementById(strat+'-'+m+'-btn');
    if(!btn) return;
    btn.className = 'mode-seg-btn' + (mode===m ? ' active-'+m : '');
  });
}

async function setStrategyMode(strat, mode){
  const statusEl = document.getElementById(strat+'-mode-status');
  const body = {}; body[strat+'_mode'] = mode;
  try {
    const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok) throw new Error(r.status);
    _applyStrategyMode(strat, mode);
    if(statusEl){ statusEl.textContent = mode==='live'?'🔴 LIVE':'paper'; setTimeout(()=>{ statusEl.textContent=''; },3000); }
  } catch(e){
    if(statusEl){ statusEl.textContent='error'; setTimeout(()=>{ statusEl.textContent=''; },3000); }
  }
}

async function saveTradeAmount(){
  const inp = document.getElementById('s-trade-amount');
  const statusEl = document.getElementById('trade-amount-status');
  const val = parseFloat(inp?.value);
  if(!inp || isNaN(val) || val < 1) { if(statusEl) statusEl.textContent='invalid'; return; }
  try {
    const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trade_amount:val})});
    if(!r.ok) throw new Error(r.status);
    if(statusEl){ statusEl.textContent='saved'; setTimeout(()=>{ statusEl.textContent=''; },3000); }
  } catch(e){
    if(statusEl){ statusEl.textContent='error'; setTimeout(()=>{ statusEl.textContent=''; },3000); }
  }
}

async function testTelegram(){
  const statusEl = document.getElementById('telegram-status');
  if(statusEl) statusEl.textContent='sending…';
  try {
    const r = await fetch('/api/test-telegram',{method:'POST'});
    const d = await r.json();
    if(statusEl){ statusEl.textContent = d.ok ? '✓ sent' : 'failed: '+(d.error||'?'); setTimeout(()=>{ statusEl.textContent=''; },4000); }
  } catch(e){
    if(statusEl){ statusEl.textContent='error'; setTimeout(()=>{ statusEl.textContent=''; },4000); }
  }
}

```

- [ ] **Step 8: Run test to verify it passes**

```bash
pytest tests/test_settings_tab.py -v
```
Expected: PASS on all settings assertions; FAIL on `test_settings_tab_absent_from_dead_dashboard` (dashboard.html still exists — that's Task 3).

- [ ] **Step 9: Commit**

```bash
git add "handoff/Money Printer.html"
git commit -m "feat(dashboard): add Settings tab with S1/S2 mode toggles, trade amount, Telegram test"
```

---

### Task 3: Delete dead `dashboard.html`

**Files:**
- Delete: `dashboard.html`
- Modify: `tests/test_settings_tab.py` (already has assertion it's gone)

**Context:** `dashboard.html` is served by nothing. `server.py` only serves `handoff/Money Printer.html`. Keeping it causes confusion — future sessions may edit the wrong file again.

- [ ] **Step 1: Verify nothing imports or serves dashboard.html**

```bash
grep -r "dashboard.html" . --include="*.py" --include="*.md" --include="*.toml" --include="*.txt" | grep -v ".venv" | grep -v __pycache__
```
Expected: no Python/config references to `dashboard.html`.

- [ ] **Step 2: Delete via git**

```bash
git rm dashboard.html
```
Expected: `rm 'dashboard.html'`

- [ ] **Step 3: Run full test suite**

```bash
pytest --tb=short -q 2>&1 | tail -20
```
Expected: all tests pass (same count as before, no test references dashboard.html).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: delete dead dashboard.html — server only serves handoff/Money Printer.html"
```

---

### Task 4: Push to GitHub

**Files:** none — git push only

- [ ] **Step 1: Confirm clean state**

```bash
git status
git log --oneline -5
```
Expected: working tree clean, 3 new commits visible.

- [ ] **Step 2: Push**

```bash
git push origin main
```
Expected: `main -> main` success.

- [ ] **Step 3: Verify on Railway**

After Railway auto-deploys (~2 min), visit `/` and confirm:
- Settings tab visible in nav
- S1/S2 mode buttons render and respond
- EV values show normal numbers (no `-1270%` or `+4890%`)

---

## Self-Review

**Spec coverage:**
- ✅ EV `*100` double-multiply bug fixed (Task 1)
- ✅ ±200 display cap added (Task 1)
- ✅ Settings tab added to live dashboard (Task 2)
- ✅ S1 + S2 mode toggles (PAPER/LIVE) in Settings (Task 2)
- ✅ Trade amount input in Settings (Task 2)
- ✅ Telegram test button in Settings (Task 2)
- ✅ `dashboard.html` deleted (Task 3)
- ✅ Pushed to GitHub (Task 4)

**Placeholder scan:** None. All code blocks are complete.

**Type consistency:** `setStrategyMode(strat, mode)` → `_applyStrategyMode(strat, mode)` — consistent. `s1-paper-btn` / `s1-live-btn` IDs match `getElementById` calls. `panel-settings` ID matches `switchTab` case.

**What is NOT in scope:**
- `notify.py` / `obs.py` — both exist and work (Read hook truncated display; PowerShell confirmed full content)
- `=0.40` — not found anywhere in repo; may have been a misremembered filename
- No changes to `server.py`, `bot_strategy.py`, `bot_loops.py`, `bot_risk.py`
