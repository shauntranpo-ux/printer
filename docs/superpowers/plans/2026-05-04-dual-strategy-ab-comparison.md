# Dual-Strategy A/B Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the original B3/E1/S1/X3/D3 per-asset strategies (commit ce9ec3f) alongside the current D3 hybrid simultaneously, with separate DB rows, Telegram labels, and dashboard P&L strips.

**Architecture:** Both strategy brains evaluate every market tick independently. S2 (D3 hybrid) runs through the full existing execution path, unchanged. S1 (original per-asset) is recovered into `src/strategies/original/`, evaluated via `strategy_brain_s1()` after S2 at each tick, and its trades are recorded via a thin shadow executor. DB outcome settlement auto-resolves all pending trades for a market when it expires. Dashboard shows two P&L strips, server gains `?strategy=` filter.

**Tech Stack:** Python (aiosqlite, aiohttp), SQLite, vanilla JS dashboard, Flask server

---

## File Map

| File | Change |
|------|--------|
| `src/strategies/original/__init__.py` | New (empty) |
| `src/strategies/original/baseline.py` | Recovered from `ce9ec3f`, no changes |
| `src/strategies/original/btc_strategy.py` | Recovered from `ce9ec3f`, signal imports rewritten |
| `src/strategies/original/eth_strategy.py` | Recovered from `ce9ec3f`, signal imports rewritten |
| `src/strategies/original/sol_strategy.py` | Recovered from `ce9ec3f`, signal imports rewritten |
| `src/strategies/original/xrp_strategy.py` | Recovered from `ce9ec3f`, signal imports rewritten |
| `src/strategies/original/doge_strategy.py` | Recovered from `ce9ec3f`, signal imports rewritten |
| `src/strategies/original/signals/__init__.py` | New (empty) |
| `src/strategies/original/signals/*.py` | 16 signal files recovered from `ce9ec3f` |
| `tests/strategies/original/test_btc_strategy.py` | Recovered from `ce9ec3f`, imports updated |
| `tests/strategies/original/test_eth_strategy.py` | Recovered from `ce9ec3f`, imports updated |
| `tests/strategies/original/test_sol_strategy.py` | Recovered from `ce9ec3f`, imports updated |
| `tests/strategies/original/test_xrp_strategy.py` | Recovered from `ce9ec3f`, imports updated |
| `tests/strategies/original/test_doge_strategy.py` | Recovered from `ce9ec3f`, imports updated |
| `bot.py` | Add `_S1_SINGLETONS`, rename S2 cache, add brain functions, shadow executor, Telegram labels |
| `server.py` | Add `?strategy=` param to `/api/trades` and `/api/pnl` |
| `handoff/Money Printer.html` | Add S1 KPI strip, update fetchPnl, add Strategy column |

---

## Task 1: Recover Original Strategy Files

**Files:**
- Create: `src/strategies/original/__init__.py`
- Create: `src/strategies/original/signals/__init__.py`
- Create: `src/strategies/original/baseline.py`
- Create: `src/strategies/original/btc_strategy.py`
- Create: `src/strategies/original/eth_strategy.py`
- Create: `src/strategies/original/sol_strategy.py`
- Create: `src/strategies/original/xrp_strategy.py`
- Create: `src/strategies/original/doge_strategy.py`
- Create: 16 files in `src/strategies/original/signals/`

- [ ] **Step 1: Create directory structure**

```powershell
New-Item -ItemType Directory -Path src\strategies\original\signals -Force
"" | Out-File -Encoding utf8 src\strategies\original\__init__.py
"" | Out-File -Encoding utf8 src\strategies\original\signals\__init__.py
```

- [ ] **Step 2: Recover baseline.py**

```powershell
git show ce9ec3f:src/strategies/baseline.py | Out-File -Encoding utf8 src\strategies\original\baseline.py
```

Verify it contains `brownian_bridge_prob_above`:
```powershell
Select-String "brownian_bridge_prob_above" src\strategies\original\baseline.py
```

Expected: at least one match.

- [ ] **Step 3: Recover 5 strategy files**

```powershell
git show ce9ec3f:src/strategies/btc_strategy.py  | Out-File -Encoding utf8 src\strategies\original\btc_strategy.py
git show ce9ec3f:src/strategies/eth_strategy.py  | Out-File -Encoding utf8 src\strategies\original\eth_strategy.py
git show ce9ec3f:src/strategies/sol_strategy.py  | Out-File -Encoding utf8 src\strategies\original\sol_strategy.py
git show ce9ec3f:src/strategies/xrp_strategy.py  | Out-File -Encoding utf8 src\strategies\original\xrp_strategy.py
git show ce9ec3f:src/strategies/doge_strategy.py | Out-File -Encoding utf8 src\strategies\original\doge_strategy.py
```

- [ ] **Step 4: Recover 16 signal files**

```powershell
$signals = @(
    "beta_cache","btc_context","btc_diurnal_obi","correlation_monitor",
    "event_calendar","exhaustion_fade","funding_dispersion",
    "idiosyncratic_detector","kalshi_velocity","ratio_divergence",
    "rolling_beta","session_awareness","session_clock","solana_health",
    "taper","variance_ratio"
)
foreach ($s in $signals) {
    git show ce9ec3f:src/strategies/signals/$s.py | Out-File -Encoding utf8 "src\strategies\original\signals\$s.py"
}
```

Verify all 16 exist:
```powershell
(Get-ChildItem src\strategies\original\signals\ -Filter "*.py" | Where-Object Name -ne "__init__.py").Count
```
Expected: `16`

- [ ] **Step 5: Rewrite signal imports in all 5 strategy files**

In each strategy file, replace `from strategies.signals.` with `from strategies.original.signals.` and `from strategies.baseline` with `from strategies.original.baseline`. Run for each file:

```powershell
foreach ($f in @("btc_strategy","eth_strategy","sol_strategy","xrp_strategy","doge_strategy")) {
    $path = "src\strategies\original\$f.py"
    $content = Get-Content $path -Raw
    $content = $content -replace 'from strategies\.signals\.', 'from strategies.original.signals.'
    $content = $content -replace 'from strategies\.baseline ', 'from strategies.original.baseline '
    $content | Out-File -Encoding utf8 $path
}
```

- [ ] **Step 6: Verify imports rewrote correctly**

```powershell
Select-String "from strategies\.signals\." src\strategies\original\*.py
```
Expected: **no matches** (all changed to `strategies.original.signals`).

```powershell
Select-String "from strategies\.original\." src\strategies\original\*.py
```
Expected: multiple matches across all 5 files.

- [ ] **Step 7: Smoke-test the import**

```powershell
& python -c "import sys; sys.path.insert(0,'src'); from strategies.original.btc_strategy import BTCStrategy; print('BTCStrategy OK')"
```
Expected: `BTCStrategy OK` with no errors.

```powershell
& python -c "import sys; sys.path.insert(0,'src'); from strategies.original.eth_strategy import ETHStrategy; from strategies.original.sol_strategy import SOLStrategy; from strategies.original.xrp_strategy import XRPStrategy; from strategies.original.doge_strategy import DOGEStrategy; print('All OK')"
```
Expected: `All OK`

- [ ] **Step 8: Commit**

```powershell
git add src\strategies\original\
git commit -m "feat: recover original B3/E1/S1/X3/D3 strategies into src/strategies/original/"
```

---

## Task 2: Recover Test Files

**Files:**
- Create: `tests/strategies/original/__init__.py`
- Create: `tests/strategies/original/test_btc_strategy.py`
- Create: `tests/strategies/original/test_eth_strategy.py`
- Create: `tests/strategies/original/test_sol_strategy.py`
- Create: `tests/strategies/original/test_xrp_strategy.py`
- Create: `tests/strategies/original/test_doge_strategy.py`

- [ ] **Step 1: Create test directory**

```powershell
New-Item -ItemType Directory -Path tests\strategies\original -Force
"" | Out-File -Encoding utf8 tests\strategies\original\__init__.py
```

- [ ] **Step 2: Recover 5 test files from ce9ec3f**

```powershell
git show ce9ec3f:tests/strategies/test_btc_strategy.py  | Out-File -Encoding utf8 tests\strategies\original\test_btc_strategy.py
git show ce9ec3f:tests/strategies/test_eth_strategy.py  | Out-File -Encoding utf8 tests\strategies\original\test_eth_strategy.py
git show ce9ec3f:tests/strategies/test_sol_strategy.py  | Out-File -Encoding utf8 tests\strategies\original\test_sol_strategy.py
git show ce9ec3f:tests/strategies/test_xrp_strategy.py  | Out-File -Encoding utf8 tests\strategies\original\test_xrp_strategy.py
git show ce9ec3f:tests/strategies/test_doge_strategy.py | Out-File -Encoding utf8 tests\strategies\original\test_doge_strategy.py
```

- [ ] **Step 3: Update imports in test files**

Replace `from strategies.btc_strategy` → `from strategies.original.btc_strategy`, etc.:

```powershell
$map = @{
    "btc_strategy"  = "btc_strategy"
    "eth_strategy"  = "eth_strategy"
    "sol_strategy"  = "sol_strategy"
    "xrp_strategy"  = "xrp_strategy"
    "doge_strategy" = "doge_strategy"
}
foreach ($key in $map.Keys) {
    $path = "tests\strategies\original\test_$key.py"
    $content = Get-Content $path -Raw
    $content = $content -replace "from strategies\.$($map[$key])", "from strategies.original.$($map[$key])"
    $content = $content -replace "from strategies\.signals\.", "from strategies.original.signals."
    $content = $content -replace "from strategies\.baseline ", "from strategies.original.baseline "
    $content | Out-File -Encoding utf8 $path
}
```

- [ ] **Step 4: Run the recovered tests**

```powershell
& python -m pytest tests\strategies\original\ -v 2>&1 | Select-Object -Last 20
```

Expected: all tests pass (the pre-existing `test_ratio_z_score_stable_ratio_is_none` failure in eth tests is out-of-scope per spec and acceptable).

- [ ] **Step 5: Run full existing test suite to confirm no regressions**

```powershell
& python -m pytest tests\ -x -q 2>&1 | Select-Object -Last 10
```

Expected: same number of passes as before (559+).

- [ ] **Step 6: Commit**

```powershell
git add tests\strategies\original\
git commit -m "feat: recover original strategy test files under tests/strategies/original/"
```

---

## Task 3: Database — strategy_variant Column

**Files:**
- Modify: `bot.py` — `init_db()` and `db_write_trade()`

- [ ] **Step 1: Add `strategy_variant` to CREATE TABLE in `init_db()`**

In `bot.py`, locate the `CREATE TABLE IF NOT EXISTS trades` block. It ends with `entry_signals TEXT`. Add the new column after it:

Find:
```python
                entry_signals         TEXT
            )
        """)
```

Replace with:
```python
                entry_signals         TEXT,
                strategy_variant      TEXT DEFAULT 'strategy2'
            )
        """)
```

- [ ] **Step 2: Add migration for existing databases**

In `init_db()`, inside the migration loop (the `for col, typedef in (...)` block), add `strategy_variant` to the tuple list:

Find:
```python
        for col, typedef in (
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),  # multi-asset support
            ("raw_p_yes",         "REAL"),                # pre-calibration P(YES wins)
            ("entry_signals",    "TEXT"),                # JSON snapshot of entry signals
            ("calibrated_p_yes",  "REAL"),               # post-calibration p_yes used in EV gate
            ("signal_name",       "TEXT"),               # which signal fired (d3_hybrid / supertrend)
        ):
```

Replace with:
```python
        for col, typedef in (
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),
            ("raw_p_yes",         "REAL"),
            ("entry_signals",    "TEXT"),
            ("calibrated_p_yes",  "REAL"),
            ("signal_name",       "TEXT"),
            ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),
        ):
```

- [ ] **Step 3: Update `db_write_trade()` INSERT to include strategy_variant**

Find the INSERT statement in `db_write_trade()`:

```python
            cur = await db.execute("""
                INSERT INTO trades (
                    ts, market_id, market_title, mode, side, contracts,
                    entry_price_cents, trade_amount_dollars, confidence_score,
                    model_prob, implied_prob, btc_price_at_entry, strike,
                    seconds_left_at_entry, fill_confirmed,
                    exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
                    order_id, asset, raw_p_yes, entry_signals
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
                trade.get("mode"), trade.get("side"), trade.get("contracts"),
                trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
                trade.get("confidence_score"), trade.get("model_prob"),
                trade.get("implied_prob"), trade.get("btc_price_at_entry"),
                trade.get("strike"), trade.get("seconds_left_at_entry"),
                trade.get("fill_confirmed"),
                trade.get("exit_price_cents"), trade.get("exit_reason"),
                trade.get("outcome", "pending"), trade.get("pnl_dollars"),
                trade.get("profit_percent"),
                trade.get("order_id"), trade.get("asset", "BTC"),
                trade.get("raw_p_yes"), trade.get("entry_signals"),
            ))
```

Replace with:
```python
            cur = await db.execute("""
                INSERT INTO trades (
                    ts, market_id, market_title, mode, side, contracts,
                    entry_price_cents, trade_amount_dollars, confidence_score,
                    model_prob, implied_prob, btc_price_at_entry, strike,
                    seconds_left_at_entry, fill_confirmed,
                    exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
                    order_id, asset, raw_p_yes, entry_signals, strategy_variant
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
                trade.get("mode"), trade.get("side"), trade.get("contracts"),
                trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
                trade.get("confidence_score"), trade.get("model_prob"),
                trade.get("implied_prob"), trade.get("btc_price_at_entry"),
                trade.get("strike"), trade.get("seconds_left_at_entry"),
                trade.get("fill_confirmed"),
                trade.get("exit_price_cents"), trade.get("exit_reason"),
                trade.get("outcome", "pending"), trade.get("pnl_dollars"),
                trade.get("profit_percent"),
                trade.get("order_id"), trade.get("asset", "BTC"),
                trade.get("raw_p_yes"), trade.get("entry_signals"),
                trade.get("strategy_variant", "strategy2"),
            ))
```

- [ ] **Step 4: Add `_db_settle_corollary_trades()` helper for S1 outcome settlement**

Add this new async function directly below `db_write_trade()`:

```python
async def _db_settle_corollary_trades(
    ticker: str, primary_id: int, exit_price: int, outcome: str, fee_rate: float
) -> None:
    """Update any non-primary pending trades for the same market when it settles."""
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            rows = await (await db.execute(
                "SELECT id, contracts, entry_price_cents FROM trades "
                "WHERE market_id=? AND outcome='pending' AND id!=?",
                (ticker, primary_id),
            )).fetchall()
            for row_id, contracts, entry_price_cents in rows:
                _p = entry_price_cents / 100.0
                _fee = math.ceil(fee_rate * contracts * _p * (1.0 - _p) * 100) / 100
                _pnl = (exit_price - entry_price_cents) * contracts / 100 - _fee
                _pct = (exit_price - entry_price_cents) / entry_price_cents * 100 \
                       if entry_price_cents else 0
                await db.execute("""
                    UPDATE trades
                    SET exit_price_cents=?, exit_reason='expiry', outcome=?,
                        pnl_dollars=?, profit_percent=?
                    WHERE id=?
                """, (exit_price, outcome, round(_pnl, 2), round(_pct, 2), row_id))
            await db.commit()
    except Exception as exc:
        log.error("_db_settle_corollary_trades error for %s: %s", ticker, exc)
```

- [ ] **Step 5: Verify migration runs without error**

```powershell
& python -c "import sys; sys.path.insert(0,'src'); import bot; bot.init_db(); import sqlite3; c = sqlite3.connect('kalshi_bot.db'); cols = [r[1] for r in c.execute('PRAGMA table_info(trades)').fetchall()]; c.close(); print('strategy_variant' in cols)"
```

Expected: `True`

- [ ] **Step 6: Run tests**

```powershell
& python -m pytest tests\ -x -q 2>&1 | Select-Object -Last 5
```

Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add bot.py
git commit -m "feat: add strategy_variant column to trades + _db_settle_corollary_trades helper"
```

---

## Task 4: Bot — Singleton Caches and Brain Functions

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Rename `_STRATEGY_SINGLETONS` → `_S2_SINGLETONS` everywhere**

In bot.py, do a global replace of `_STRATEGY_SINGLETONS` → `_S2_SINGLETONS`:

```powershell
$content = Get-Content bot.py -Raw
$content = $content -replace '_STRATEGY_SINGLETONS', '_S2_SINGLETONS'
$content | Out-File -Encoding utf8 bot.py
```

- [ ] **Step 2: Add `_S1_SINGLETONS` cache dict right after `_S2_SINGLETONS` declaration**

Find the line (now renamed):
```python
_S2_SINGLETONS: dict = {}  # keyed by asset name
```

Replace with:
```python
_S2_SINGLETONS: dict = {}  # keyed by asset name — current D3 hybrid (strategy2)
_S1_SINGLETONS: dict = {}  # keyed by asset name — original per-asset (strategy1)
```

- [ ] **Step 3: Rename `strategy_brain()` → `strategy_brain_s2()`**

Find:
```python
def strategy_brain(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    min_ev_base=3.0, vol_gate_thresh=1.80, kalshi_fee=0.07,
    asset="BTC", max_entry_price_cents=100.0,
    min_reward_cents=0.0, max_risk_reward_ratio=999.0,
):
    """Dispatch to FifteenMinStrategy via Supertrend. Returns brain dict."""
```

Replace with:
```python
def strategy_brain_s2(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    min_ev_base=3.0, vol_gate_thresh=1.80, kalshi_fee=0.07,
    asset="BTC", max_entry_price_cents=100.0,
    min_reward_cents=0.0, max_risk_reward_ratio=999.0,
):
    """D3 hybrid: dispatch to FifteenMinStrategy. Returns brain dict tagged strategy2."""
```

Also add `"strategy_variant": "strategy2"` to the brain dict return values inside the function. There are two return paths: the "no strategy" path (skip) and the main path. In the "no strategy" path, find:

```python
        return {
            "action": "skip",
            "side": "yes" if _above else "no",
```

Add `"strategy_variant": "strategy2",` after `"action": "skip",`.

For the main path (successful feature build), the function returns `brain_dict`. Find where that dict is constructed and returned — look for the final return statement in `strategy_brain_s2` which returns a dict including `"action"` — add `"strategy_variant": "strategy2"` to it.

- [ ] **Step 4: Add `_get_or_make_strategy_s1()` function**

Add this function directly after the `_get_or_make_strategy()` function (around line 1700 area, just after the last line of `_get_or_make_strategy`):

```python
def _get_or_make_strategy_s1(asset: str, config):
    """Lazily construct the original per-asset strategy singleton for S1."""
    import sys as _sys
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if _src not in _sys.path:
        for _k in [k for k in _sys.modules if k == "strategies" or k.startswith("strategies.")]:
            del _sys.modules[_k]
        _sys.path.insert(0, _src)

    cache_key = asset
    if cache_key in _S1_SINGLETONS:
        return _S1_SINGLETONS[cache_key]
    try:
        from strategies.skip_layer import SkipConfig
        from strategies.signals.time_windows import get_window_params, get_trading_window
        import time as _tw_now

        skip_cfg = SkipConfig(
            max_spread_cents=float(get_asset_config(config, asset, "max_spread_cents", 3.0)),
            min_seconds_left=float(config.get("min_seconds_left", 30.0)),
            min_entry_price_cents=float(config.get("min_entry_price_cents", 20.0)),
            max_entry_price_cents=float(config.get("max_entry_price_cents", 76.0)),
            cold_start_samples=int(config.get("cold_start_samples", 60)),
            vol_ratio_threshold=float(get_asset_config(config, asset, "vol_gate_thresh", 1.80)),
        )
        overrides = config.get("asset_overrides", {}).get(asset, {})
        _ev_default = config.get("min_ev_base_15m", config.get("min_ev_base", 8))
        min_ev = float(overrides.get("min_ev_base", _ev_default)) / 100.0
        stake = float(config.get("trade_amount_dollars", 25))

        _ASSET_STRATEGY_MAP = {
            "BTC":  ("strategies.original.btc_strategy",  "BTCStrategy"),
            "ETH":  ("strategies.original.eth_strategy",  "ETHStrategy"),
            "SOL":  ("strategies.original.sol_strategy",  "SOLStrategy"),
            "XRP":  ("strategies.original.xrp_strategy",  "XRPStrategy"),
            "DOGE": ("strategies.original.doge_strategy", "DOGEStrategy"),
        }
        if asset not in _ASSET_STRATEGY_MAP:
            log.warning("S1: no original strategy for asset %s", asset)
            return None
        mod_name, cls_name = _ASSET_STRATEGY_MAP[asset]
        import importlib
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        strat = cls(asset=asset, skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        _S1_SINGLETONS[cache_key] = strat
        log.info("S1 Strategy initialized: %s (%s)", cache_key, cls_name)
        return strat
    except Exception as exc:
        log.warning("S1 strategy init failed for %s: %s", asset, exc)
        return None
```

- [ ] **Step 5: Add `strategy_brain_s1()` function**

Add this directly after `strategy_brain_s2()`:

```python
def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    min_ev_base=3.0, vol_gate_thresh=1.80, kalshi_fee=0.07,
    asset="BTC", max_entry_price_cents=100.0,
    min_reward_cents=0.0, max_risk_reward_ratio=999.0,
):
    """Original per-asset strategies (B3/E1/S1/X3/D3). Returns brain dict tagged strategy1."""
    config = read_config()
    strat = _get_or_make_strategy_s1(asset, config)
    if strat is None:
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip",
            "side": "yes" if _above else "no",
            "confidence": 50,
            "reasoning": f"s1_no_strategy:{asset}",
            "key_signals": [],
            "signals": {},
            "win_prob": 0.5,
            "mom_label": "s1_no_strategy",
            "mom_pct": 0.0,
            "vel_signal": "neutral",
            "raw_p_yes": None,
            "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above,
            "_rv": None,
            "_vol_ratio": None,
            "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    from strategies.feature_builder import build_features_from_bot_state
    try:
        features = build_features_from_bot_state(
            asset=asset,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            no_ask=no_ask,
            elapsed_seconds=elapsed_seconds,
            secs_left=secs_left,
            ticker=ticker,
        )
        decision = strat.decide(features)
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": decision.action,
            "side": decision.side or ("yes" if _above else "no"),
            "confidence": int((decision.p_model or 0.5) * 100),
            "reasoning": decision.reason or "",
            "key_signals": [],
            "signals": decision.contributing_signals or {},
            "win_prob": decision.p_model or 0.5,
            "mom_label": "s1_original",
            "mom_pct": 0.0,
            "vel_signal": "neutral",
            "raw_p_yes": (decision.contributing_signals or {}).get("raw_p_yes"),
            "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above,
            "_rv": None,
            "_vol_ratio": None,
            "price_filter_skip": False,
            "expected_value": decision.expected_value,
            "strategy_variant": "strategy1",
        }
    except Exception as exc:
        log.warning("S1 brain error for %s: %s", asset, exc)
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip",
            "side": "yes" if _above else "no",
            "confidence": 50,
            "reasoning": f"s1_error:{exc}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "s1_error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": 0.0, "above": _above, "_rv": None, "_vol_ratio": None,
            "price_filter_skip": False, "strategy_variant": "strategy1",
        }
```

- [ ] **Step 6: Update the two `strategy_brain()` call sites**

**Call site 1 (multi-window selection loop, around line 2724):**

Find:
```python
                        c_brain = strategy_brain(
```

Replace with:
```python
                        c_brain = strategy_brain_s2(
```

**Call site 2 (main decision point, around line 2827):**

Find:
```python
    # ── Printer Brain – primary decision engine (always runs, no API needed) ──
    brain = strategy_brain(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                     min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
                     vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
                     kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                     max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
                     min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
                     max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
                     asset=asset)
```

Replace with:
```python
    # ── Strategy brains — S2 (D3 hybrid) and S1 (original per-asset) run independently ──
    _brain_kwargs = dict(
        min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
        vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
        kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
        max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
        min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
        max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
        asset=asset,
    )
    brain_s1 = strategy_brain_s1(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, **_brain_kwargs)
    brain = strategy_brain_s2(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, **_brain_kwargs)
```

- [ ] **Step 7: Verify startup imports**

```powershell
& python -c "import sys; sys.path.insert(0,'src'); import bot; print('imports OK')"
```

Expected: `imports OK` with no errors.

- [ ] **Step 8: Run tests**

```powershell
& python -m pytest tests\ -x -q 2>&1 | Select-Object -Last 5
```

Expected: all pass.

- [ ] **Step 9: Commit**

```powershell
git add bot.py
git commit -m "feat: add _S1_SINGLETONS, strategy_brain_s1/s2, dual-brain call at ready phase"
```

---

## Task 5: Bot — S1 Shadow Execution and Settlement

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Add `_s1_pending_trades` global tracking dict**

Find the `_S1_SINGLETONS` declaration line. Add after it:

```python
_s1_pending_trades: dict = {}  # ticker → {"trade_id": int, "side": str, "contracts": int, "entry_price_cents": int, "mode": str, "asset": str}
```

- [ ] **Step 2: Add `_execute_s1_shadow()` async function**

Add this function directly before `handle_ready_phase()` (around line 2650):

```python
async def _execute_s1_shadow(
    brain_s1: dict,
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    secs_left: float,
    ticker: str,
    ob: dict,
    market: dict,
    config: dict,
    asset: str,
    mode: str,
) -> None:
    """Record an S1 shadow trade entry when the original strategy signals a trade."""
    global _s1_pending_trades
    if brain_s1.get("action") != "trade":
        log.info("S1 [%s] skip: %s", asset, brain_s1.get("reasoning", "no signal"))
        return

    s1_side = brain_s1.get("side", "yes")
    s1_entry = yes_ask if s1_side == "yes" else no_ask
    s1_avail = ob.get("yes_liquidity", 999) if s1_side == "yes" else ob.get("no_liquidity", 999)
    trade_amount = float(config.get("trade_amount_dollars", 25))
    s1_contracts, s1_dollars = calculate_contracts(trade_amount, int(s1_entry), s1_avail)
    if s1_contracts == 0:
        log.info("S1 [%s] skip: insufficient liquidity for shadow trade", asset)
        return

    try:
        _market_elapsed = seconds_elapsed(market) if market else 0.0
    except Exception:
        _market_elapsed = 0.0
    _market_dur_min = (_market_elapsed + float(secs_left)) / 60.0

    trade_data = {
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "market_id":            ticker,
        "market_title":         market.get("title", "") if market else "",
        "mode":                 mode,
        "side":                 s1_side,
        "contracts":            s1_contracts,
        "entry_price_cents":    int(s1_entry),
        "trade_amount_dollars": round(s1_dollars, 2),
        "confidence_score":     brain_s1.get("confidence", 50),
        "model_prob":           brain_s1.get("win_prob", 0.5),
        "implied_prob":         s1_entry / 100.0,
        "btc_price_at_entry":   btc_price,
        "strike":               strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed":       1,
        "exit_price_cents":     None,
        "exit_reason":          None,
        "outcome":              "pending",
        "pnl_dollars":          None,
        "profit_percent":       None,
        "order_id":             None,
        "asset":                asset,
        "raw_p_yes":            brain_s1.get("raw_p_yes"),
        "entry_signals":        json.dumps(brain_s1.get("signals", {})),
        "strategy_variant":     "strategy1",
    }
    trade_id = await db_write_trade(trade_data)

    if trade_id:
        _s1_pending_trades[ticker] = {
            "trade_id":          trade_id,
            "side":              s1_side,
            "contracts":         s1_contracts,
            "entry_price_cents": int(s1_entry),
            "mode":              mode,
            "asset":             asset,
            "market_duration_min": _market_dur_min,
        }

    _sv_label = "[S1 Original]"
    _s1_win_pct = int(brain_s1.get("win_prob", 0.5) * 100)
    _s1_cost = round(int(s1_entry) * s1_contracts / 100, 2)
    _s1_payout = round((100 - int(s1_entry)) * s1_contracts / 100, 2)
    _s1_fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
    _s1_p = int(s1_entry) / 100.0
    _s1_ev = round((brain_s1.get("win_prob", 0.5) - _s1_p - _s1_fee_rate * (1.0 - _s1_p)) * 100, 1)
    _s1_ev_str = f"+{_s1_ev}%" if _s1_ev >= 0 else f"{_s1_ev}%"
    _s1_ctx = _notify_ctx(asset, ticker, _market_dur_min, _phase_for_eth(asset, _market_elapsed))
    mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    _time_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _expiry_dt = datetime.now(timezone(timedelta(hours=-7))) + timedelta(seconds=secs_left)
    await send_telegram(
        f"<b>{_sv_label} {_s1_ctx} {mode_icon} ORDER FILLED</b>  —  {_time_str}\n"
        f"<b>{s1_side.upper()} — {'UP' if s1_side == 'yes' else 'DOWN'}</b>  {s1_contracts} contracts @ <b>{int(s1_entry)}c</b>\n"
        f"Cost: ${_s1_cost:.2f}  |  Max payout: ${_s1_payout:.2f}\n"
        f"Win prob: {_s1_win_pct}%  |  EV: {_s1_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s -> {_expiry_dt.strftime('%I:%M %p PST')}"
    )
    log.info("S1 [%s] shadow trade written: id=%s side=%s %dx @ %dc", asset, trade_id, s1_side, s1_contracts, int(s1_entry))
```

- [ ] **Step 3: Call `_execute_s1_shadow()` after brain_s1 is computed in `handle_ready_phase()`**

In `handle_ready_phase()`, immediately after the block that was changed in Task 4 Step 6 (the `_brain_kwargs` / `brain_s1` / `brain` lines), add:

```python
    # S1 shadow evaluation — runs independently of S2's execution path
    asyncio.create_task(_execute_s1_shadow(
        brain_s1, btc_price, strike, yes_ask, no_ask,
        secs_left, ticker, ob, market, config, asset, mode,
    ))
```

Note: `ob` is available after the orderbook fetch block that precedes line 2827. If `ob` is still None at this point (possible only in race conditions), `_execute_s1_shadow` accesses `ob.get(...)` which would raise — add a guard: `if ob is not None:` around the asyncio.create_task call.

- [ ] **Step 4: Add `strategy_variant` to the S2 position dict**

In `handle_ready_phase()`, in the `_new_position` dict construction (around line 3052), find:

```python
    _new_position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "mode": mode,
        "strike": strike,
        "entry_ts": _entry_ts,
        "market_duration_min": _market_duration_min,
        "elapsed_at_entry": _market_elapsed_at_entry,
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "asset": asset,
    }
```

Replace with:

```python
    _new_position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "mode": mode,
        "strike": strike,
        "entry_ts": _entry_ts,
        "market_duration_min": _market_duration_min,
        "elapsed_at_entry": _market_elapsed_at_entry,
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "asset": asset,
        "strategy_variant": "strategy2",
    }
```

Also add `"strategy_variant": "strategy2"` to the trade_data dict (around line 3020 where trade_data is constructed) so the S2 trade record correctly tags itself. Find the trade_data dict end (right before `trade_id = await db_write_trade(trade_data)`) and add the key:

```python
        "raw_p_yes":         brain.get("raw_p_yes"),
        "entry_signals":    json.dumps({...}),
        "strategy_variant":  "strategy2",
    }
```

- [ ] **Step 5: Add S1 settlement and Telegram outcome in `handle_locked_phase()`**

In `handle_locked_phase()`, after the `await db_update_trade(pos["trade_id"], {...})` call (around line 3194), add:

```python
        # Settle any co-running strategy trades for the same market (e.g., S1 shadow)
        _fee_rate_settle = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
        await _db_settle_corollary_trades(ticker, pos["trade_id"], exit_price, outcome, _fee_rate_settle)

        # S1 outcome Telegram
        global _s1_pending_trades
        _s1_pos = _s1_pending_trades.pop(ticker, None)
        if _s1_pos:
            _s1_mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(_s1_pos["mode"], "[LIVE]")
            _s1_outcome = "win" if (
                (_s1_pos["side"] == "yes" and outcome == "win") or
                (_s1_pos["side"] == "no"  and outcome == "win")
            ) else "loss"
            _s1_pnl_sign = 1 if _s1_outcome == "win" else -1
            _s1_ep = _s1_pos["entry_price_cents"]
            _s1_exit = 100 if _s1_outcome == "win" else 0
            _s1_fee_r = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
            _s1_p = _s1_ep / 100.0
            _s1_fee = math.ceil(_s1_fee_r * _s1_pos["contracts"] * _s1_p * (1.0 - _s1_p) * 100) / 100
            _s1_pnl = (_s1_exit - _s1_ep) * _s1_pos["contracts"] / 100 - _s1_fee
            _s1_pct = (_s1_exit - _s1_ep) / _s1_ep * 100 if _s1_ep else 0
            _s1_pnl_str = f"+${_s1_pnl:.2f}" if _s1_pnl >= 0 else f"-${abs(_s1_pnl):.2f}"
            _s1_out_str = "WIN" if _s1_outcome == "win" else "LOSS"
            _s1_ctx = _notify_ctx(_s1_pos["asset"], ticker, _s1_pos.get("market_duration_min", 15.0))
            await send_telegram(
                f"<b>[S1 Original] {_s1_ctx} {_s1_mode_icon} {_s1_out_str}  {_s1_pnl_str}  ({_s1_pct:+.0f}%)</b>\n"
                f"{_s1_pos['side'].upper()}  {_s1_pos['contracts']} contracts\n"
                f"Entry: {_s1_ep}c  ->  Expiry: {_s1_exit}c"
            )
```

- [ ] **Step 6: Add `[S2 D3 Hybrid]` label to S2's Telegram notifications**

**Entry fill notification (line ~3094 in `handle_ready_phase`):**

Find:
```python
    await send_telegram(
        f"<b>{_fill_ctx} {mode_icon} {_strat_tag}</b>  —  {_time_str}\n"
```

Replace with:
```python
    await send_telegram(
        f"<b>[S2 D3 Hybrid] {_fill_ctx} {mode_icon} {_strat_tag}</b>  —  {_time_str}\n"
```

**Outcome notification (line ~3253 in `handle_locked_phase`):**

Find:
```python
        await send_telegram(
            f"<b>{_close_ctx} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
```

Replace with:
```python
        await send_telegram(
            f"<b>[S2 D3 Hybrid] {_close_ctx} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
```

- [ ] **Step 7: Run full test suite**

```powershell
& python -m pytest tests\ -x -q 2>&1 | Select-Object -Last 10
```

Expected: all pass.

- [ ] **Step 8: Commit**

```powershell
git add bot.py
git commit -m "feat: S1 shadow executor, settlement, and strategy Telegram labels"
```

---

## Task 6: Server — strategy Filter Parameters

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Add `?strategy=` filter to `/api/trades`**

In `server.py`, find the `api_trades()` function (around line 268). It currently reads `mode` and `asset` params. Add `strategy`:

Find:
```python
    mode  = request.args.get("mode")
    asset = request.args.get("asset", "").upper() or None
    try:
        conn = get_db()
        if mode and asset:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode=? AND asset=? ORDER BY ts DESC LIMIT 500",
                (mode, asset),
            ).fetchall()
        elif mode:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode=? ORDER BY ts DESC LIMIT 500",
                (mode,),
            ).fetchall()
        elif asset:
            rows = conn.execute(
                "SELECT * FROM trades WHERE asset=? ORDER BY ts DESC LIMIT 500",
                (asset,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT 500"
            ).fetchall()
```

Replace with:
```python
    mode     = request.args.get("mode")
    asset    = request.args.get("asset", "").upper() or None
    strategy = request.args.get("strategy")
    sv_map   = {"1": "strategy1", "2": "strategy2"}
    sv_val   = sv_map.get(strategy)
    try:
        conn = get_db()
        filters = []
        params  = []
        if mode:
            filters.append("mode=?"); params.append(mode)
        if asset:
            filters.append("asset=?"); params.append(asset)
        if sv_val:
            filters.append("strategy_variant=?"); params.append(sv_val)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY ts DESC LIMIT 500",
            params,
        ).fetchall()
```

- [ ] **Step 2: Add `?strategy=` filter and `by_strategy` key to `/api/pnl`**

In `server.py`, find `api_pnl()` (around line 501). 

After `today = datetime.now(timezone.utc).strftime("%Y-%m-%d")`, add:

```python
        strategy  = request.args.get("strategy")
        sv_map    = {"1": "strategy1", "2": "strategy2"}
        sv_filter = sv_map.get(strategy)
```

After `all_trades = [dict(r) for r in rows]`, add filtering:

```python
        if sv_filter:
            all_trades = [t for t in all_trades if t.get("strategy_variant") == sv_filter]
```

After the existing `return jsonify({...})` dict (before the closing `}`), add the `by_strategy` key only when no strategy filter is active:

Find the return statement:
```python
        return jsonify({
            "today": {
                "live":     today_live,
                "paper":    today_paper,
                "demo":     today_demo,
                "by_asset": today_by_asset,
                "date":     today,
            },
            "alltime": {
                "live":  alltime_live,
                "paper": alltime_paper,
                "demo":  alltime_demo,
            },
        })
```

Replace with:
```python
        response = {
            "today": {
                "live":     today_live,
                "paper":    today_paper,
                "demo":     today_demo,
                "by_asset": today_by_asset,
                "date":     today,
            },
            "alltime": {
                "live":  alltime_live,
                "paper": alltime_paper,
                "demo":  alltime_demo,
            },
        }
        if not sv_filter:
            for sv_key in ("strategy1", "strategy2"):
                sv_trades = [t for t in all_trades if t.get("strategy_variant") == sv_key]
                sv_today  = [t for t in sv_trades if (t.get("ts") or "").startswith(today)]
                response.setdefault("by_strategy", {})[sv_key] = {
                    "today":   _pnl(sv_today),
                    "alltime": _pnl(sv_trades),
                }
        return jsonify(response)
```

- [ ] **Step 3: Test the endpoints**

Start the server (if it isn't running) and run:

```powershell
# Verify by_strategy appears in unfiltered response
Invoke-RestMethod http://localhost:8000/api/pnl | ConvertTo-Json -Depth 5 | Select-String "by_strategy"

# Verify strategy filter works
Invoke-RestMethod "http://localhost:8000/api/pnl?strategy=2" | ConvertTo-Json -Depth 3 | Select-String "alltime"
Invoke-RestMethod "http://localhost:8000/api/pnl?strategy=1" | ConvertTo-Json -Depth 3 | Select-String "alltime"
```

If server is not running, skip live test — the logic is straightforward and will be verified when running the bot.

- [ ] **Step 4: Run tests**

```powershell
& python -m pytest tests\ -x -q 2>&1 | Select-Object -Last 5
```

- [ ] **Step 5: Commit**

```powershell
git add server.py
git commit -m "feat: add ?strategy= filter to /api/trades and /api/pnl with by_strategy key"
```

---

## Task 7: Dashboard — Dual P&L Strips

**Files:**
- Modify: `handoff/Money Printer.html`

- [ ] **Step 1: Rename existing KPI strip label and add S2 header**

The existing KPI strip is at approximately line 655. Find:

```html
  <!-- KPI STRIP -->
  <div class="kpi-strip">
    <div class="kpi-cell">
      <div class="kpi-cell-l">Net P&amp;L · All-Time<span class="badge mode-badge">PAPER</span></div>
```

Replace with:
```html
  <!-- KPI STRIP — Strategy 2: D3 Hybrid -->
  <div style="font-size:11px;font-weight:600;color:var(--ink-3);letter-spacing:.06em;margin-bottom:4px;padding-left:2px;">STRATEGY 2 — D3 HYBRID</div>
  <div class="kpi-strip" id="kpi-strip-s2">
    <div class="kpi-cell">
      <div class="kpi-cell-l">Net P&amp;L · All-Time<span class="badge mode-badge">PAPER</span></div>
```

- [ ] **Step 2: Add the closing `</div>` for the renamed strip and insert S1 strip below**

Find the end of the existing KPI strip (the `</div>` that closes `<div class="kpi-strip">`). It ends after the third `kpi-cell` block. The existing strip looks like:

```html
    </div>
  </div>

  <!-- MARKET STRIP -->
```

Replace with:
```html
    </div>
  </div>

  <!-- KPI STRIP — Strategy 1: Original (B3/E1/S1/X3/D3) -->
  <div style="font-size:11px;font-weight:600;color:var(--amber);letter-spacing:.06em;margin-top:12px;margin-bottom:4px;padding-left:2px;">STRATEGY 1 — ORIGINAL (B3/E1/S1/X3/D3)</div>
  <div class="kpi-strip" id="kpi-strip-s1" style="border-color:var(--amber);--accent:var(--amber);--accent-soft:var(--amber-soft);">
    <div class="kpi-cell">
      <div class="kpi-cell-l">Net P&amp;L · All-Time<span class="badge mode-badge">PAPER</span></div>
      <div id="s1-kpi-alltime-val" class="kpi-cell-v mono">—</div>
      <div id="s1-kpi-alltime-sub" class="kpi-cell-s">—</div>
    </div>
    <div class="kpi-div"></div>
    <div class="kpi-cell">
      <div class="kpi-cell-l">Win Rate · All-Time</div>
      <div id="s1-kpi-wr-val" class="kpi-cell-v mono">—<span style="color:var(--ink-3);font-size:16px;font-weight:500;">%</span></div>
      <div id="s1-kpi-wr-sub" class="kpi-cell-s">— trades</div>
    </div>
    <div class="kpi-div"></div>
    <div class="kpi-cell">
      <div class="kpi-cell-l">Today's P&amp;L<span class="badge mode-badge">PAPER</span></div>
      <div id="s1-kpi-today-val" class="kpi-cell-v mono">—</div>
      <div id="s1-kpi-today-sub" class="kpi-cell-s">— trades</div>
    </div>
  </div>

  <!-- MARKET STRIP -->
```

- [ ] **Step 3: Update `fetchPnl()` to use `?strategy=2` and add `fetchPnlS1()`**

Find the `fetchPnl()` function (around line 1841):

```javascript
async function fetchPnl() {
  try {
    const d    = await fetch('/api/pnl').then(r => r.json());
```

Replace with:

```javascript
async function fetchPnl() {
  try {
    const d    = await fetch('/api/pnl?strategy=2').then(r => r.json());
```

Then add a new `fetchPnlS1()` function directly after `fetchPnl()` closes (after the `} catch(e) {}` line and the closing `}`):

```javascript
async function fetchPnlS1() {
  try {
    const d    = await fetch('/api/pnl?strategy=1').then(r => r.json());
    const mode = document.body.dataset.mode || 'paper';
    const today   = d.today?.[mode]   || {};
    const alltime = d.alltime?.[mode] || {};

    const atVal = document.getElementById('s1-kpi-alltime-val');
    if (atVal) {
      const v = alltime.pnl ?? 0;
      atVal.className   = `kpi-cell-v mono ${v >= 0 ? 'up' : 'down'}`;
      atVal.textContent = fmtMoney(v);
    }
    const wrVal = document.getElementById('s1-kpi-wr-val');
    if (wrVal) {
      const wr = alltime.win_rate ?? 0;
      wrVal.innerHTML = `${wr.toFixed(1)}<span style="color:var(--ink-3);font-size:16px;font-weight:500;">%</span>`;
    }
    const wrSub = document.getElementById('s1-kpi-wr-sub');
    if (wrSub) wrSub.innerHTML = `<span class="mono">${alltime.trades ?? 0} trades</span>`;
    const tdVal = document.getElementById('s1-kpi-today-val');
    if (tdVal) {
      const v = today.pnl ?? 0;
      tdVal.className   = `kpi-cell-v mono ${v >= 0 ? 'up' : 'down'}`;
      tdVal.textContent = fmtMoney(v);
    }
    const tdSub = document.getElementById('s1-kpi-today-sub');
    if (tdSub) {
      const w = today.wins ?? 0, cnt = today.trades ?? 0, l = cnt - w;
      tdSub.innerHTML = `<span class="mono">${cnt} trades</span> · ${w}W / ${l}L`;
    }
  } catch(e) {}
}
```

- [ ] **Step 4: Add `fetchPnlS1()` to the startup and poll cadence**

Find:
```javascript
  fetchPnl();
```

Replace with:
```javascript
  fetchPnl();
  fetchPnlS1();
```

Find:
```javascript
setInterval(fetchPnl,         60_000)
```

Replace with:
```javascript
setInterval(fetchPnl,         60_000);
setInterval(fetchPnlS1,       60_000);
```

- [ ] **Step 5: Add Strategy column to the trades table**

In the trades table header, find the header row (look for `<th>` elements in the trades table — search for "Direction" or "Entry" column headers). Add a `Strategy` column header.

Search for the trades table header structure and add `<th>Strategy</th>` after the `Time` column header.

In the trades table row rendering (in the JavaScript that builds trade rows — look for `TRADES.map` or the function that renders trade rows), add a cell that shows `S1` or `S2` as a pill badge based on `t.strategy_variant`:

```javascript
`<td><span class="badge" style="background:${t.strategy_variant==='strategy1'?'var(--amber-soft)':'var(--up-soft)};color:${t.strategy_variant==='strategy1'?'var(--amber)':'var(--up)'}">${t.strategy_variant==='strategy1'?'S1':'S2'}</span></td>`
```

- [ ] **Step 6: Open dashboard in browser and verify**

Open `handoff/Money Printer.html` in a browser. Confirm:
- Two separate P&L strips visible (S2 in blue, S1 in amber)
- Both labeled clearly
- Trades table shows S1/S2 pill badges

- [ ] **Step 7: Commit**

```powershell
git add "handoff/Money Printer.html"
git commit -m "feat: dual strategy P&L strips on dashboard, S1/S2 trade badges"
```

---

## Task 8: End-to-End Verification

- [ ] **Step 1: Import check**

```powershell
& python -c "import sys; sys.path.insert(0,'src'); from strategies.original.btc_strategy import BTCStrategy; print('OK')"
```
Expected: `OK`

- [ ] **Step 2: Full test suite**

```powershell
& python -m pytest tests\ -q 2>&1 | Select-Object -Last 5
```
Expected: all existing tests pass.

- [ ] **Step 3: DB schema check**

```powershell
& python -c "
import sqlite3, bot
bot.init_db()
c = sqlite3.connect('kalshi_bot.db')
cols = [r[1] for r in c.execute('PRAGMA table_info(trades)').fetchall()]
c.close()
print('strategy_variant in cols:', 'strategy_variant' in cols)
print('Columns:', [col for col in cols if 'strategy' in col])
"
```
Expected: `strategy_variant in cols: True`

- [ ] **Step 4: Verify strategy singletons log on startup**

Start the bot (brief paper mode) and check logs:
```
S1 Strategy initialized: BTC (BTCStrategy)
S2 Strategy initialized: BTC (15m, ...)
```

- [ ] **Step 5: Verify dual DB rows after one cycle**

After one or two paper trading cycles:
```powershell
& python -c "
import sqlite3
c = sqlite3.connect('kalshi_bot.db')
rows = c.execute('SELECT strategy_variant, COUNT(*) FROM trades GROUP BY strategy_variant').fetchall()
c.close()
print(rows)
"
```
Expected: rows for both `strategy1` and `strategy2`.

- [ ] **Step 6: API verification**

```powershell
Invoke-RestMethod "http://localhost:8000/api/pnl" | ConvertTo-Json -Depth 4 | Select-String "by_strategy"
```
Expected: response contains `by_strategy` with `strategy1` and `strategy2` keys.

- [ ] **Step 7: Final commit and summary**

```powershell
git log --oneline -8
```

All 8 verification steps from the spec should be confirmed.

---

## Spec Coverage Self-Check

| Spec Requirement | Plan Task |
|-----------------|-----------|
| Files in `src/strategies/original/` | Task 1 |
| Import path fix `strategies.signals.*` → `strategies.original.signals.*` | Task 1 Step 5 |
| Test files recovered from ce9ec3f | Task 2 |
| `strategy_variant` column in DB | Task 3 |
| `init_db()` migration | Task 3 Step 2 |
| `db_write_trade()` gains `strategy_variant` | Task 3 Step 3 |
| `_S2_SINGLETONS` / `_S1_SINGLETONS` caches | Task 4 |
| `_get_or_make_strategy_s1()` function | Task 4 Step 4 |
| `strategy_brain_s1()` function | Task 4 Step 5 |
| `strategy_brain_s2()` rename | Task 4 Step 3 |
| Both brains called at market tick | Task 4 Step 6 |
| S1 trade logged with `strategy_variant='strategy1'` | Task 5 Steps 1–3 |
| S1 outcome settled on market expiry | Task 5 Steps 4–5 |
| `[S1 Original]` / `[S2 D3 Hybrid]` Telegram labels | Task 5 Step 6 |
| `/api/trades?strategy=` param | Task 6 Step 1 |
| `/api/pnl?strategy=` + `by_strategy` key | Task 6 Step 2 |
| Dashboard S2 strip renamed | Task 7 Step 1 |
| Dashboard S1 strip added (amber) | Task 7 Step 2 |
| `fetchPnlS1()` polls `/api/pnl?strategy=1` | Task 7 Steps 3–4 |
| Strategy column in trades table | Task 7 Step 5 |
