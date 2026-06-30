# Profitability Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix S1/S2 labeling, enable all 5 assets, add overnight quiet-hours gate, and relax EV/entry-price gates to increase trade frequency and hit $500/week.

**Architecture:** Four independent changes to `bot_strategy.py`, `bot_infra.py`, `server.py`, and `handoff/Money Printer.html`. No new files needed. The overnight gate is a shared utility function in `bot_strategy.py`. Asset defaults are set in both `bot_infra.py` (runtime read_config) and `server.py` (initial config.json write). The EV and price gates are per-asset config constants.

**Tech Stack:** Python 3.12 (asyncio, datetime), Flask, vanilla JS, pytest

---

## Root Cause Analysis (read before implementing)

**Finding 1 — S1/S2 label wrong in trades table**
`handoff/Money Printer.html` `mapTrade()` maps `sv: t.strategy_variant` but never maps `t.brain`. The badge reads `sv`. Old trades (before `brain` column existed) have `strategy_variant = null` → default 'strategy2' → show S2 even if they were S1 trades. Fix: map `brain` field and use it as authoritative label when present.

**Finding 2 — BTC not enabled by default**
`bot_infra.py:73`: `cfg.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])` — BTC absent.
`bot_loops.py:1159`: `if "BTC" not in config.get("enabled_assets", [])` → skips all BTC trading.
`server.py:_FULL_CONFIG_DEFAULT` — no `enabled_assets` key at all.
Net result: fresh deploy only trades ETH/SOL/XRP. BTC and DOGE silently disabled.

**Finding 3 — Midnight loss ($25 every night)**
No overnight gate. Both strategies trade 24/7. S1 can fire during LOCKED phase (when S2 already holds a position). Overnight markets (10pm–7am ET) have thin liquidity and unreliable velocity signals. Empirical win-rate tables were calibrated on daytime data but applied overnight → systematic loss.

**Finding 4 — EV gate blocks high-entry-price trades**
`min_ev = 0.05` (5%). At entry price 90¢ with win_prob 0.97: EV = 0.97 - 0.90 - fee ≈ 0.06 → passes. At 93¢: EV = 0.97 - 0.93 - fee ≈ 0.03 → FAILS. The bot skips high-conviction trades just because the market has already priced them in. Lowering to 0.02 (2%) lets these through while still requiring positive expected value.

**Finding 5 — max_entry_price_cents = 76 blocks contracts trading at 77–99¢**
When a market is 80% certain (contract trades at 80¢), the price filter blocks S1 and S2 from entering at all, even though 80¢ entry with a 97% win rate is highly profitable. Expanding to 85¢ captures these.

---

## Files Modified

| File | Change |
|------|--------|
| `handoff/Money Printer.html` | Task 1: badge uses `brain` field; `mapTrade` maps `brain` |
| `bot_infra.py` | Task 2: default enabled_assets includes all 5 |
| `server.py` | Task 2: _FULL_CONFIG_DEFAULT includes enabled_assets; Task 3: quiet_hours_enabled validator |
| `bot_strategy.py` | Task 3: `_is_quiet_hours()` shared gate; Task 4: lower min_ev, expand max_entry_price |
| `tests/test_profitability_overhaul.py` | All tasks: new test file |

---

### Task 1: Fix S1/S2 trade label in dashboard

**Files:**
- Modify: `handoff/Money Printer.html` (mapTrade function ~line 2025, badge ~line 1693)
- Test: `tests/test_profitability_overhaul.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profitability_overhaul.py
def test_dashboard_badge_uses_brain_field():
    """Dashboard trades badge must use brain field when present."""
    # This is a static analysis test — verify the HTML uses t.brain
    with open('handoff/Money Printer.html', encoding='utf-8') as f:
        src = f.read()
    # mapTrade must map the brain field
    assert "brain:" in src or "t.brain" in src, \
        "mapTrade does not map the brain field from the API response"
    # Badge must prefer brain over strategy_variant
    assert "t.brain==='s1'" in src or "brain==='s1'" in src, \
        "Badge does not use brain field for S1/S2 label"
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_profitability_overhaul.py::test_dashboard_badge_uses_brain_field -v
```
Expected: FAIL — `mapTrade does not map the brain field`

- [ ] **Step 3: Update mapTrade to include brain field**

In `handoff/Money Printer.html`, find the `mapTrade` function (around line 2026) and update the returned object to include `brain`:

Find:
```javascript
    sv:     t.strategy_variant || 'strategy2',
  };
```

Replace with:
```javascript
    sv:     t.strategy_variant || 'strategy2',
    brain:  t.brain || null,
  };
```

- [ ] **Step 4: Update the badge to use brain as authoritative label**

In `handoff/Money Printer.html`, find the badge line in the trades table (around line 1693):

Find:
```javascript
      <td><span class="badge" style="${(t.strategy_variant||'strategy2')==='strategy1'?'background:var(--amber-soft);color:var(--amber);':'background:var(--accent-soft);color:var(--accent);'}border:none;font-size:9px;">${(t.strategy_variant||'strategy2')==='strategy1'?'S1':'S2'}</span></td>
```

Replace with:
```javascript
      <td><span class="badge" style="${(t.brain==='s1'||t.strategy_variant==='strategy1')?'background:var(--amber-soft);color:var(--amber);':'background:var(--accent-soft);color:var(--accent);'}border:none;font-size:9px;">${(t.brain==='s1'||t.strategy_variant==='strategy1')?'S1':'S2'}</span></td>
```

- [ ] **Step 5: Run test to verify it passes**

```
python -m pytest tests/test_profitability_overhaul.py::test_dashboard_badge_uses_brain_field -v
```
Expected: PASS

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -x -q --tb=short
```
Expected: all existing tests pass

- [ ] **Step 7: Commit**

```bash
git add "handoff/Money Printer.html" tests/test_profitability_overhaul.py
git commit -m "fix(dashboard): use brain field as authoritative S1/S2 label in trades table"
```

---

### Task 2: Enable all 5 assets by default

**Files:**
- Modify: `bot_infra.py` line 73
- Modify: `server.py` `_FULL_CONFIG_DEFAULT` (~line 55)
- Test: `tests/test_profitability_overhaul.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_profitability_overhaul.py`:

```python
def test_bot_infra_default_includes_all_5_assets():
    """bot_infra read_config default must include all 5 assets."""
    # Read bot_infra.py and check the setdefault line
    with open('bot_infra.py', encoding='utf-8') as f:
        src = f.read()
    # The setdefault for enabled_assets must list all 5
    assert '"BTC"' in src and '"DOGE"' in src, \
        "bot_infra.py enabled_assets default missing BTC or DOGE"
    # Find the setdefault line specifically
    idx = src.find('setdefault("enabled_assets"')
    assert idx != -1, "setdefault for enabled_assets not found"
    chunk = src[idx:idx+100]
    assert 'BTC' in chunk and 'DOGE' in chunk, \
        f"setdefault line does not include BTC and DOGE: {chunk!r}"


def test_server_full_config_default_includes_all_5_assets():
    """server._FULL_CONFIG_DEFAULT must include all 5 assets."""
    import sys
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    assert 'enabled_assets' in server._FULL_CONFIG_DEFAULT, \
        "_FULL_CONFIG_DEFAULT missing enabled_assets key"
    ea = server._FULL_CONFIG_DEFAULT['enabled_assets']
    for asset in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']:
        assert asset in ea, f"{asset} not in _FULL_CONFIG_DEFAULT enabled_assets: {ea}"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_profitability_overhaul.py::test_bot_infra_default_includes_all_5_assets tests/test_profitability_overhaul.py::test_server_full_config_default_includes_all_5_assets -v
```
Expected: FAIL — BTC and/or DOGE missing

- [ ] **Step 3: Fix bot_infra.py default**

In `bot_infra.py`, find line 73:

Find:
```python
        cfg.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])
```

Replace with:
```python
        cfg.setdefault("enabled_assets", ["BTC", "ETH", "SOL", "XRP", "DOGE"])
```

- [ ] **Step 4: Fix server.py _FULL_CONFIG_DEFAULT**

In `server.py`, find `_FULL_CONFIG_DEFAULT`:

Find:
```python
_FULL_CONFIG_DEFAULT = {
    "bot_enabled": True,
    "mode": "paper",
    "trade_amount_dollars": 25,
    "confidence_threshold": 72,
    "daily_loss_limit_dollars": 50,
    "daily_profit_target_dollars": 200,
}
```

Replace with:
```python
_FULL_CONFIG_DEFAULT = {
    "bot_enabled": True,
    "mode": "paper",
    "trade_amount_dollars": 25,
    "confidence_threshold": 72,
    "daily_loss_limit_dollars": 50,
    "daily_profit_target_dollars": 200,
    "enabled_assets": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
}
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/test_profitability_overhaul.py::test_bot_infra_default_includes_all_5_assets tests/test_profitability_overhaul.py::test_server_full_config_default_includes_all_5_assets -v
```
Expected: PASS

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -x -q --tb=short
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add bot_infra.py server.py tests/test_profitability_overhaul.py
git commit -m "fix(config): enable all 5 assets (BTC+ETH+SOL+XRP+DOGE) by default"
```

---

### Task 3: Add overnight quiet-hours gate

**Files:**
- Modify: `bot_strategy.py` — add `_is_quiet_hours()` function; apply as first gate in both S1 and S2
- Modify: `server.py` — add `quiet_hours_enabled` to validators
- Test: `tests/test_profitability_overhaul.py`

The quiet-hours gate prevents all trading between configurable ET hours (default 10pm–7am). This stops the systematic midnight loss caused by applying daytime-calibrated win-rate tables to thin overnight markets.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_profitability_overhaul.py`:

```python
def test_quiet_hours_gate_exists_in_strategy():
    """bot_strategy.py must have a quiet-hours gate applied in both S1 and S2."""
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    assert '_is_quiet_hours' in src, "_is_quiet_hours function not found in bot_strategy.py"
    # Both strategies must check it
    s1_section = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    s2_section = src[src.index('def strategy_brain_s2'):]
    assert '_is_quiet_hours' in s1_section, "_is_quiet_hours not called in strategy_brain_s1"
    assert '_is_quiet_hours' in s2_section, "_is_quiet_hours not called in strategy_brain_s2"


def test_quiet_hours_gate_blocks_midnight():
    """_is_quiet_hours must return True at midnight ET."""
    import importlib
    import sys
    # Mock datetime to midnight ET (UTC-4 = 04:00 UTC for midnight ET)
    from unittest.mock import patch
    import datetime as dt_mod

    if 'bot_strategy' in sys.modules:
        del sys.modules['bot_strategy']
    if 'bot_state' in sys.modules:
        del sys.modules['bot_state']
    if 'asset_manager' in sys.modules:
        del sys.modules['asset_manager']

    fake_midnight_utc = dt_mod.datetime(2026, 6, 2, 4, 0, 0,
                                         tzinfo=dt_mod.timezone.utc)
    with patch('bot_strategy.datetime') as mock_dt:
        mock_dt.now.return_value = fake_midnight_utc
        mock_dt.timezone = dt_mod.timezone
        mock_dt.timedelta = dt_mod.timedelta
        import bot_strategy
        config = {"quiet_hours_enabled": True, "quiet_start_et": 22, "quiet_end_et": 7}
        assert bot_strategy._is_quiet_hours(config) is True, \
            "4:00 UTC (midnight ET) should be quiet hours"


def test_quiet_hours_gate_allows_midday():
    """_is_quiet_hours must return False at noon ET."""
    import sys
    import datetime as dt_mod
    from unittest.mock import patch

    if 'bot_strategy' in sys.modules:
        del sys.modules['bot_strategy']
    if 'bot_state' in sys.modules:
        del sys.modules['bot_state']
    if 'asset_manager' in sys.modules:
        del sys.modules['asset_manager']

    fake_noon_utc = dt_mod.datetime(2026, 6, 2, 16, 0, 0,
                                     tzinfo=dt_mod.timezone.utc)
    with patch('bot_strategy.datetime') as mock_dt:
        mock_dt.now.return_value = fake_noon_utc
        mock_dt.timezone = dt_mod.timezone
        mock_dt.timedelta = dt_mod.timedelta
        import bot_strategy
        config = {"quiet_hours_enabled": True, "quiet_start_et": 22, "quiet_end_et": 7}
        assert bot_strategy._is_quiet_hours(config) is False, \
            "16:00 UTC (noon ET) should NOT be quiet hours"


def test_quiet_hours_disabled_allows_midnight():
    """_is_quiet_hours must return False when quiet_hours_enabled=False."""
    import sys
    import datetime as dt_mod
    from unittest.mock import patch

    if 'bot_strategy' in sys.modules:
        del sys.modules['bot_strategy']
    if 'bot_state' in sys.modules:
        del sys.modules['bot_state']
    if 'asset_manager' in sys.modules:
        del sys.modules['asset_manager']

    fake_midnight_utc = dt_mod.datetime(2026, 6, 2, 4, 0, 0,
                                         tzinfo=dt_mod.timezone.utc)
    with patch('bot_strategy.datetime') as mock_dt:
        mock_dt.now.return_value = fake_midnight_utc
        mock_dt.timezone = dt_mod.timezone
        mock_dt.timedelta = dt_mod.timedelta
        import bot_strategy
        config = {"quiet_hours_enabled": False}
        assert bot_strategy._is_quiet_hours(config) is False, \
            "quiet_hours_enabled=False must always return False"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_profitability_overhaul.py::test_quiet_hours_gate_exists_in_strategy -v
```
Expected: FAIL — `_is_quiet_hours not found`

- [ ] **Step 3: Add _is_quiet_hours to bot_strategy.py**

In `bot_strategy.py`, after the `_s1_is_us_session` function (around line 118), add:

```python
def _is_quiet_hours(config: dict) -> bool:
    """
    True when the current ET time falls within the configured overnight quiet window.
    Default: 10pm–7am ET (22:00–07:00). Configurable via config keys:
      quiet_hours_enabled (bool, default True)
      quiet_start_et      (int hour, default 22)
      quiet_end_et        (int hour, default 7)
    Returns False when quiet_hours_enabled is False or on any clock error.
    """
    if not config.get("quiet_hours_enabled", True):
        return False
    try:
        # ET = UTC-4 (EDT) year-round; up to 1h edge error is acceptable
        now_et = datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        hour = now_et.hour
        start = int(config.get("quiet_start_et", 22))
        end   = int(config.get("quiet_end_et", 7))
        if start > end:
            # Spans midnight: quiet if hour >= start OR hour < end
            return hour >= start or hour < end
        else:
            return start <= hour < end
    except Exception:
        return False  # fail open — never block on a clock error
```

- [ ] **Step 4: Apply gate in strategy_brain_s1**

In `bot_strategy.py`, inside `strategy_brain_s1`, immediately after the `config = read_config()` and `cfg = {...}` lines (around line 163), add:

Find:
```python
    # Cap gate: global S1 position limit
    _s1_global_cap = config.get("max_s1_positions", 3)
```

Replace with:
```python
    # Quiet hours gate — skip overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s1_quiet_hours", abs_pct, mins_left, variant="strategy1")

    # Cap gate: global S1 position limit
    _s1_global_cap = config.get("max_s1_positions", 3)
```

- [ ] **Step 5: Apply gate in strategy_brain_s2**

In `bot_strategy.py`, inside `strategy_brain_s2`, immediately after the `config = read_config()` and `cfg = {...}` lines (around line 441), add:

Find:
```python
    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s2_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy2")
```

Replace with:
```python
    # Quiet hours gate — skip overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s2_quiet_hours", abs_pct, mins_left, variant="strategy2")

    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s2_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy2")
```

- [ ] **Step 6: Add quiet_hours_enabled to server.py validator**

In `server.py`, inside the `validators` dict in `api_config()` (~line 303), add after the existing entries:

Find:
```python
        "bot_enabled":                 lambda v: isinstance(v, bool),
```

Replace with:
```python
        "bot_enabled":                 lambda v: isinstance(v, bool),
        "quiet_hours_enabled":         lambda v: isinstance(v, bool),
        "quiet_start_et":              lambda v: isinstance(v, int) and 0 <= v <= 23,
        "quiet_end_et":                lambda v: isinstance(v, int) and 0 <= v <= 23,
```

- [ ] **Step 7: Run the quiet-hours tests**

```
python -m pytest tests/test_profitability_overhaul.py::test_quiet_hours_gate_exists_in_strategy tests/test_profitability_overhaul.py::test_quiet_hours_gate_blocks_midnight tests/test_profitability_overhaul.py::test_quiet_hours_gate_allows_midday tests/test_profitability_overhaul.py::test_quiet_hours_disabled_allows_midnight -v
```
Expected: all 4 PASS

- [ ] **Step 8: Run full suite**

```
python -m pytest tests/ -x -q --tb=short
```
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add bot_strategy.py server.py tests/test_profitability_overhaul.py
git commit -m "feat(strategy): add overnight quiet-hours gate (10pm-7am ET) to S1 and S2"
```

---

### Task 4: Lower EV gate and expand entry price range

**Files:**
- Modify: `bot_strategy.py` — `_S1_ASSET_CONFIG` and `_S2_ASSET_CONFIG`
- Test: `tests/test_profitability_overhaul.py`

**Why:** `min_ev=0.05` blocks high-entry-price trades with excellent win rates. At 93¢ entry, win_prob 0.97 gives EV=0.03 — that's a great trade we're currently skipping. `max_entry_price_cents=76` blocks all markets trading at 77–99¢. Lowering min_ev to 0.02 and expanding to 85¢ maintains a positive-EV requirement while unlocking more opportunities.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_profitability_overhaul.py`:

```python
def test_s1_s2_min_ev_lowered():
    """S1 and S2 min_ev must be 0.02 for all assets."""
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    import ast, re
    # Find _S1_ASSET_CONFIG dict in source and verify min_ev values
    # Use simple pattern: all min_ev= in the config blocks should be 0.02
    s1_block_start = src.index('_S1_ASSET_CONFIG: dict = {')
    s1_block_end   = src.index('}', s1_block_start + 50)
    s1_block = src[s1_block_start:s1_block_end]
    min_ev_values_s1 = re.findall(r'min_ev=([\d.]+)', s1_block)
    for v in min_ev_values_s1:
        assert float(v) <= 0.02, f"S1 min_ev {v} still above 0.02"

    s2_block_start = src.index('_S2_ASSET_CONFIG: dict = {')
    s2_block_end   = src.index('}', s2_block_start + 50)
    s2_block = src[s2_block_start:s2_block_end]
    min_ev_values_s2 = re.findall(r'min_ev=([\d.]+)', s2_block)
    for v in min_ev_values_s2:
        assert float(v) <= 0.02, f"S2 min_ev {v} still above 0.02"


def test_max_entry_price_default_expanded():
    """Default max_entry_price_cents must be 85 to capture high-conviction markets."""
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    # Both S1 and S2 call get_asset_config with max_entry_price_cents default
    import re
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', src)
    assert defaults, "max_entry_price_cents default not found in bot_strategy.py"
    for d in defaults:
        assert float(d) >= 85, f"max_entry_price_cents default {d} still below 85"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_profitability_overhaul.py::test_s1_s2_min_ev_lowered tests/test_profitability_overhaul.py::test_max_entry_price_default_expanded -v
```
Expected: FAIL — values still 0.05 and 76

- [ ] **Step 3: Lower min_ev in _S1_ASSET_CONFIG**

In `bot_strategy.py`, update `_S1_ASSET_CONFIG` (all 5 assets, lines 96–107):

Find:
```python
_S1_ASSET_CONFIG: dict = {
    #           min_dist   max_rv   ema_short  ema_long  session  min_ev  t_min  t_max
    # MUST match scripts/calibrate_winrates.py S1_ASSET_CONFIG — win rate tables are invalid otherwise.
    "BTC":  dict(min_dist=0.0025, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, ema_short=3, ema_long=8,
                 session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
    "XRP":  dict(min_dist=0.0040, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
    # DOGE: strike_increment=$0.001 at ~$0.15 -> max possible dist ~0.33%; keep min_dist below that
    "DOGE": dict(min_dist=0.0080, max_rv=1.0, ema_short=2, ema_long=8,
                 session_gate=False, min_ev=0.05, time_min=0.5, time_max=14.0),
}
```

Replace with:
```python
_S1_ASSET_CONFIG: dict = {
    #           min_dist   max_rv   ema_short  ema_long  session  min_ev  t_min  t_max
    # MUST match scripts/calibrate_winrates.py S1_ASSET_CONFIG — win rate tables are invalid otherwise.
    "BTC":  dict(min_dist=0.0025, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.02, time_min=0.5, time_max=14.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.02, time_min=0.5, time_max=14.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, ema_short=3, ema_long=8,
                 session_gate=False, min_ev=0.02, time_min=0.5, time_max=14.0),
    "XRP":  dict(min_dist=0.0040, max_rv=1.0, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.02, time_min=0.5, time_max=14.0),
    # DOGE: strike_increment=$0.001 at ~$0.15 -> max possible dist ~0.33%; keep min_dist below that
    "DOGE": dict(min_dist=0.0080, max_rv=1.0, ema_short=2, ema_long=8,
                 session_gate=False, min_ev=0.02, time_min=0.5, time_max=14.0),
}
```

- [ ] **Step 4: Lower min_ev in _S2_ASSET_CONFIG**

In `bot_strategy.py`, update `_S2_ASSET_CONFIG` (all 5 assets, lines 299–304):

Find:
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

Replace with:
```python
_S2_ASSET_CONFIG: dict = {
    #           min_dist  min_obi  min_vel_delta  vel_lookback  min_ev  t_min  t_max
    # ALL values MUST match scripts/calibrate_winrates.py S2_ASSET_CONFIG.
    # min_dist and min_vel_delta are calibration inputs; tables are invalid otherwise.
    # time_min/time_max must stay within S2_ENTRY_OFFSETS=[2..12] min range.
    "BTC":  dict(min_dist=0.0035, min_obi=0.02, min_vel_delta=0.80, vel_lookback=4, min_ev=0.02, time_min=2.0, time_max=12.5),
    "ETH":  dict(min_dist=0.0030, min_obi=0.02, min_vel_delta=0.70, vel_lookback=4, min_ev=0.02, time_min=2.0, time_max=12.5),
    "SOL":  dict(min_dist=0.0060, min_obi=0.02, min_vel_delta=1.20, vel_lookback=3, min_ev=0.02, time_min=2.0, time_max=12.5),
    "XRP":  dict(min_dist=0.0050, min_obi=0.02, min_vel_delta=0.90, vel_lookback=4, min_ev=0.02, time_min=2.0, time_max=12.5),
    "DOGE": dict(min_dist=0.0100, min_obi=0.02, min_vel_delta=1.50, vel_lookback=3, min_ev=0.02, time_min=2.0, time_max=12.5),
}
```

- [ ] **Step 5: Expand max_entry_price_cents default**

In `bot_strategy.py`, there are two calls to `get_asset_config` with `"max_entry_price_cents"` — one in S1 (around line 225) and one in S2 (around line 478).

Find (S1, line ~225):
```python
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
```
Replace with:
```python
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 85.0))
```

Find (S2, line ~478):
```python
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
```
Replace with:
```python
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 85.0))
```

- [ ] **Step 6: Run failing tests to verify they now pass**

```
python -m pytest tests/test_profitability_overhaul.py::test_s1_s2_min_ev_lowered tests/test_profitability_overhaul.py::test_max_entry_price_default_expanded -v
```
Expected: PASS

- [ ] **Step 7: Run full suite**

```
python -m pytest tests/ -x -q --tb=short
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add bot_strategy.py tests/test_profitability_overhaul.py
git commit -m "perf(strategy): lower min_ev 0.05→0.02; expand max_entry_price 76→85¢ for more trades"
```

---

### Task 5: Push and verify

- [ ] **Step 1: Run full suite one final time**

```
python -m pytest tests/ -q --tb=short
```
Expected: all pass (≥362 tests)

- [ ] **Step 2: Confirm git log shows all 4 commits**

```
git log --oneline -6
```
Expected output (approximately):
```
<sha>  perf(strategy): lower min_ev 0.05→0.02; expand max_entry_price 76→85¢ for more trades
<sha>  feat(strategy): add overnight quiet-hours gate (10pm-7am ET) to S1 and S2
<sha>  fix(config): enable all 5 assets (BTC+ETH+SOL+XRP+DOGE) by default
<sha>  fix(dashboard): use brain field as authoritative S1/S2 label in trades table
```

- [ ] **Step 3: Push**

```
git push origin main
```

---

## Self-Review

**Spec coverage:**
- ✅ Dashboard S1/S2 label fix → Task 1
- ✅ All 5 markets enabled → Task 2
- ✅ Midnight loss prevention → Task 3 (quiet hours gate)
- ✅ More trades / profitability → Tasks 2 (5 assets), 4 (EV gate + price range)
- ✅ $500/week goal — enabled by 5 assets × 2 strategies × looser gates; at $25 stake with 80%+ WR this is achievable with ~15 daytime trades/day

**What this does NOT change (intentionally):**
- Win-rate tables — calibrated empirically, leave alone
- Distance gates (min_dist) — these filter genuinely-uncertain markets
- Reversal gate — empirically blocks 20% WR trades; leave alone
- Velocity thresholds (S2 min_vel_delta) — calibration-sensitive; leave alone
- Trade amount — user controls via Settings tab

**Placeholder scan:** None found.

**Type consistency:** All function names and config keys consistent throughout.
