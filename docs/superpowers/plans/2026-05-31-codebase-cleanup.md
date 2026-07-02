# Codebase Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove dead code, fix critical config defaults, clean up unused dependencies, and move offline scripts out of the project root.

**Architecture:** Surgical edits across 4–5 files with no logic changes to the trading path. Each task is self-contained. Order matters only for Task 5 (requirements) which should come after Task 4 (scripts relocated).

**Tech Stack:** Python, sqlite3, Flask, Railway deployment

---

## Pre-audit findings (do not re-audit — trust these)

| Claim | Verified? | Finding |
|-------|-----------|---------|
| Strike regex `\*` bug | ❌ No | Regex is correct; `*` is a quantifier, not `\*` literal |
| `trades` missing `asset`/`strategy_variant` | ❌ No | Both in CREATE TABLE |
| `trades` missing `brain` | ✅ Yes | In ALTER TABLE only — missing from CREATE TABLE |
| `aiosqlite` unused | ❌ No | Used in `bot_infra.py` — keep it |
| `httpx` unused | ✅ Yes | Not imported by any live file |
| `requests` unused in live | ✅ Yes | Only in offline scripts |
| `pyarrow` unused | ✅ Yes | Not imported anywhere |
| `scikit-learn` unused in live | ✅ Yes | Only in dead `strategies/` quarantine |
| `anthropic` missing | ✅ Yes | `weekly_report.py` docstring says Claude-powered; not in requirements |
| `/new` = duplicate of `/` | ✅ Yes | Identical handler body |
| `_all_markets_cache_ts` dead | ✅ Yes | Set in `bot_market.py:401`, never read |
| `daily_loss_limit_dollars: 50000` | ✅ Yes | `server.py` default; bot_infra caps at 150 but only after writing 50000 |

---

## File Map

| File | Action |
|------|--------|
| `server.py` | Fix loss-limit default (50000→50); fix profit-target (50000→200); remove `/new` route |
| `bot_state.py` | Remove `_all_markets_cache_ts` variable and from `__all__` list |
| `bot_market.py` | Remove the `_all_markets_cache_ts` assignment |
| `bot_infra.py` | Add `brain TEXT` to CREATE TABLE statement |
| `requirements.txt` | Remove httpx, requests, pyarrow, scikit-learn; add anthropic>=0.25 |
| `price_validator.py` | `git rm` (offline tool, only run locally) |
| `validate_and_report.py` | `git rm` (offline tool) |
| `collect_kalshi_ladder_history.py` | `git rm` (offline tool, uses requests) |
| `weekly_report.py` | `git rm` (offline tool; not yet Claude-integrated) |

---

### Task 1: Fix `daily_loss_limit_dollars` and `daily_profit_target_dollars` defaults in server.py

**Files:**
- Modify: `server.py` (two separate blocks near lines 57–62 and 115–117)

**Context:** `server.py` writes a fresh `config.json` on first deploy with `daily_loss_limit_dollars: 50000`. This means the loss limit never fires until `bot_infra.py` overwrites it on next restart (caps at 150). Similarly, `daily_profit_target_dollars: 50000` is unreachable. Fix both to sane values: 50 and 200.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_config_defaults.py
import importlib, sys

def test_loss_limit_default_is_sane():
    """server.py initial config default must not be 50000."""
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    default = server._FULL_CONFIG_DEFAULT
    assert default["daily_loss_limit_dollars"] <= 200, (
        f"daily_loss_limit_dollars default is {default['daily_loss_limit_dollars']} — way too high"
    )
    assert default["daily_profit_target_dollars"] <= 500, (
        f"daily_profit_target_dollars default is {default['daily_profit_target_dollars']} — way too high"
    )

def test_config_sentinel_default_is_sane():
    """_CONFIG_DEFAULT in server.py must not have 50000 loss limit."""
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    sentinel = server._CONFIG_DEFAULT
    assert sentinel["daily_loss_limit_dollars"] <= 200
    assert sentinel["daily_profit_target_dollars"] <= 500
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_server_config_defaults.py -v
```
Expected: FAIL — both asserts fail (current value is 50000).

- [ ] **Step 3: Fix `_FULL_CONFIG_DEFAULT` block**

Read `server.py` around line 54–63 to get exact text. The block looks like:
```python
_FULL_CONFIG_DEFAULT = {
    "bot_enabled": True,
    "mode": "paper",
    "trade_amount_dollars": 25,
    "confidence_threshold": 72,
    "daily_loss_limit_dollars": 50000,
    "daily_profit_target_dollars": 50000,
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
}
```

- [ ] **Step 4: Fix `_CONFIG_DEFAULT` sentinel block**

Read `server.py` around line 114–117. The line looks like:
```python
_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 25, "confidence_threshold": 72,
                   "daily_loss_limit_dollars": 50000,
                   "daily_profit_target_dollars": 50000}
```
Replace with:
```python
_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 25, "confidence_threshold": 72,
                   "daily_loss_limit_dollars": 50,
                   "daily_profit_target_dollars": 200}
```

- [ ] **Step 5: Run test to verify it passes**

```
pytest tests/test_server_config_defaults.py -v
```
Expected: PASS.

- [ ] **Step 6: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_server_config_defaults.py
git commit -m "fix(config): lower daily_loss_limit default from 50000 to 50; profit_target from 50000 to 200"
```

---

### Task 2: Remove duplicate `/new` route from server.py

**Files:**
- Modify: `server.py`

**Context:** Both `@app.route("/")` (`index()`) and `@app.route("/new")` (`new_dashboard()`) are identical — they both open `handoff/Money Printer.html` and return it. `/new` was a legacy route from when the old dashboard was at `/` and the new one at `/new`. Now they're the same file. Remove `/new`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_routes.py
def test_no_new_route():
    """The /new route must not exist — it's a duplicate of /."""
    import server
    rules = [str(r) for r in server.app.url_map.iter_rules()]
    assert "/new" not in rules, f"/new route still exists: {rules}"
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_server_routes.py::test_no_new_route -v
```
Expected: FAIL — `/new` exists.

- [ ] **Step 3: Remove `new_dashboard` function**

Read `server.py` to find the exact `@app.route("/new")` block. It looks like:
```python
@app.route("/new")
def new_dashboard():
    """Serve the Money Printer dashboard."""
    try:
        path = os.path.join(_BASE_DIR, "handoff", "Money Printer.html")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        from flask import Response
        return Response(content, mimetype="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    except Exception as exc:
        log.error(f"new_dashboard() failed: {exc}", exc_info=True)
        return f"<h1>Error: {exc}</h1>", 500
```
Delete the entire `@app.route("/new")` and `new_dashboard()` function.

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_server_routes.py -v
```
Expected: PASS.

- [ ] **Step 5: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server_routes.py
git commit -m "chore: remove duplicate /new route (identical to /)"
```

---

### Task 3: Remove `_all_markets_cache_ts` dead variable

**Files:**
- Modify: `bot_state.py` (declaration + `__all__`)
- Modify: `bot_market.py` (the one write assignment)

**Context:** `_all_markets_cache_ts: float = 0.0` is declared in `bot_state.py` and set once in `bot_market.py:401` but never read anywhere. It's dead state.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dead_code_removed.py
def test_all_markets_cache_ts_removed():
    """_all_markets_cache_ts must not exist — it was set but never read."""
    import bot_state
    assert not hasattr(bot_state, '_all_markets_cache_ts'), (
        "_all_markets_cache_ts still exists in bot_state"
    )
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_dead_code_removed.py::test_all_markets_cache_ts_removed -v
```
Expected: FAIL — attribute exists.

- [ ] **Step 3: Remove from bot_state.py `__all__`**

Read `bot_state.py` lines 18–22. Find the `"_all_markets_cache_ts"` entry in the `__all__` list and remove it.

Current (approximate):
```python
    "_market_cache", "_market_cache_ts", "_all_markets_cache", "_all_markets_cache_ts",
```
Replace with:
```python
    "_market_cache", "_market_cache_ts", "_all_markets_cache",
```

- [ ] **Step 4: Remove the variable declaration from bot_state.py**

Find line ~69:
```python
_all_markets_cache_ts: float = 0.0
```
Delete that line.

- [ ] **Step 5: Remove the assignment from bot_market.py**

Find line ~401:
```python
    bot_state._all_markets_cache_ts = now
```
Delete that line.

- [ ] **Step 6: Run test to verify it passes**

```
pytest tests/test_dead_code_removed.py -v
```
Expected: PASS.

- [ ] **Step 7: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot_state.py bot_market.py tests/test_dead_code_removed.py
git commit -m "chore: remove _all_markets_cache_ts dead variable (set but never read)"
```

---

### Task 4: Add `brain` column to CREATE TABLE in bot_infra.py

**Files:**
- Modify: `bot_infra.py` (CREATE TABLE statement around line 175–203)

**Context:** `brain TEXT` is added via ALTER TABLE migration (line 271) but is absent from the CREATE TABLE definition. A fresh DB creation would include `brain` via the migration loop, but having it in CREATE TABLE makes the schema authoritative and avoids any edge case where the loop is skipped. No data loss risk — ALTER TABLE already adds it to existing DBs.

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_dead_code_removed.py (or new file)
import sqlite3

def test_brain_column_in_create_table():
    """CREATE TABLE in bot_infra must include brain column, not just ALTER TABLE."""
    with open('bot_infra.py', encoding='utf-8') as f:
        src = f.read()
    # Find the CREATE TABLE IF NOT EXISTS trades block
    start = src.index('CREATE TABLE IF NOT EXISTS trades')
    end = src.index(')', src.index('"brain"', start) if '"brain"' in src[start:start+2000] else start)
    # Simpler: just check the string appears in the CREATE TABLE block
    create_block = src[start:start+2000]
    assert 'brain' in create_block and create_block.index('brain') < create_block.index(')'), \
        "brain column not in CREATE TABLE block"
```

Actually, easier to run the schema directly:

```python
# tests/test_trades_schema.py
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_brain_column_in_fresh_db():
    """A freshly created trades table must have a brain column."""
    import bot_infra
    import asyncio, tempfile
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        # Monkey-patch the DB path and run init
        original = bot_infra._DB_PATH if hasattr(bot_infra, '_DB_PATH') else None
        # Directly create schema via sqlite3
        conn = sqlite3.connect(db_path)
        # Extract and run the CREATE TABLE from the source
        import inspect, textwrap
        src = inspect.getsource(bot_infra.init_db)
        # Just verify the column is declared in the source
        assert 'brain' in src and 'CREATE TABLE IF NOT EXISTS trades' in src, \
            "brain column missing from init_db CREATE TABLE"
        # Confirm it appears before the closing paren of CREATE TABLE
        create_start = src.index('CREATE TABLE IF NOT EXISTS trades')
        create_end = src.index(')\n        """)', create_start)
        create_block = src[create_start:create_end]
        assert 'brain' in create_block, \
            f"brain column not in CREATE TABLE block. Block:\n{create_block}"
        conn.close()
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_trades_schema.py -v
```
Expected: FAIL — `brain` not in CREATE TABLE block.

- [ ] **Step 3: Add `brain TEXT` to CREATE TABLE**

Read `bot_infra.py` lines 175–204. The CREATE TABLE ends with:
```python
                raw_p_yes             REAL,
                entry_signals         TEXT,
                strategy_variant      TEXT DEFAULT 'strategy2'
            )
```
Change to:
```python
                raw_p_yes             REAL,
                entry_signals         TEXT,
                strategy_variant      TEXT DEFAULT 'strategy2',
                brain                 TEXT
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_trades_schema.py -v
```
Expected: PASS.

- [ ] **Step 5: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass (ALTER TABLE migration still handles existing DBs).

- [ ] **Step 6: Commit**

```bash
git add bot_infra.py tests/test_trades_schema.py
git commit -m "fix(db): add brain column to CREATE TABLE (was only in ALTER TABLE migration)"
```

---

### Task 5: Clean up requirements.txt

**Files:**
- Modify: `requirements.txt`

**Context:**
- **Remove:** `httpx` (not imported by any live file), `requests` (only offline scripts), `pyarrow` (not imported anywhere), `scikit-learn` (only in dead `strategies/` quarantine)
- **Add:** `anthropic>=0.25` (needed to implement Claude-powered weekly_report.py; and is an Anthropic SDK)
- **Keep:** `aiosqlite` (used by `bot_infra.py`)

Note: `requests` removal is safe because the offline scripts (`collect_kalshi_ladder_history.py`, `scripts/calibrate_winrates.py`) that use it are not run in the Railway production environment.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_requirements.py
def test_no_unused_heavy_deps():
    """requirements.txt must not include packages unused by live bot."""
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'httpx' not in reqs, "httpx is unused in live code"
    assert 'pyarrow' not in reqs, "pyarrow is unused in live code"
    assert 'scikit-learn' not in reqs, "scikit-learn is only in dead quarantine code"

def test_anthropic_in_requirements():
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'anthropic' in reqs, "anthropic SDK missing from requirements.txt"

def test_aiosqlite_present():
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'aiosqlite' in reqs, "aiosqlite must stay — used by bot_infra.py"
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_requirements.py -v
```
Expected: FAIL — httpx/pyarrow/scikit-learn present, anthropic absent.

- [ ] **Step 3: Rewrite requirements.txt**

Replace the entire contents with:
```
aiohttp>=3.9
aiosqlite>=0.20
websockets>=12.0
cryptography>=42.0
flask>=3.0
gunicorn>=21.0
python-dotenv>=1.0
tzdata>=2024.1
numpy>=1.24
pandas>=2.0
anthropic>=0.25
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_requirements.py -v
```
Expected: PASS.

- [ ] **Step 5: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass (no live code imports any of the removed packages).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt tests/test_requirements.py
git commit -m "chore(deps): remove unused httpx/requests/pyarrow/scikit-learn; add anthropic"
```

---

### Task 6: Remove offline scripts from repo root

**Files:**
- Delete: `price_validator.py`, `validate_and_report.py`, `collect_kalshi_ladder_history.py`, `weekly_report.py`

**Context:** These 4 scripts are standalone offline analysis tools. They were placed in the repo root but are never imported by the live bot (`bot.py`, `runner.py`, `server.py`). They should not be deployed to Railway — they pollute the root with irrelevant files and create confusion about what's live. Permanently removing them from git tracking is the cleanest fix. The analysis work they do can be recovered from git history if needed.

- [ ] **Step 1: Verify none are imported by live code**

```bash
grep -rn "import price_validator\|import validate_and_report\|import collect_kalshi\|import weekly_report" \
  bot.py runner.py server.py bot_loops.py bot_risk.py bot_strategy.py bot_infra.py bot_market.py
```
Expected: no output (none imported).

- [ ] **Step 2: Remove from git tracking**

```bash
git rm price_validator.py validate_and_report.py collect_kalshi_ladder_history.py weekly_report.py
```
Expected:
```
rm 'collect_kalshi_ladder_history.py'
rm 'price_validator.py'
rm 'validate_and_report.py'
rm 'weekly_report.py'
```

- [ ] **Step 3: Run full suite**

```
pytest --tb=short -q 2>&1 | tail -5
```
Expected: all tests pass (no test imports those files).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: remove offline analysis scripts from repo root (not deployed to Railway)"
```

---

### Task 7: Push to GitHub

- [ ] **Step 1: Verify clean state**

```bash
git status
git log --oneline -8
```
Expected: clean working tree, 6 new commits visible.

- [ ] **Step 2: Push**

```bash
git push origin main
```
Expected: success.

- [ ] **Step 3: Report commit list**

Run `git log --oneline -8` and include output in your response.

---

## Self-Review

**Spec coverage:**
- ✅ `daily_loss_limit_dollars` default fixed (Task 1)
- ✅ `/new` duplicate removed (Task 2)
- ✅ `_all_markets_cache_ts` dead code removed (Task 3)
- ✅ `brain` column added to CREATE TABLE (Task 4)
- ✅ `httpx`, `requests`, `pyarrow`, `scikit-learn` removed from requirements (Task 5)
- ✅ `anthropic` added to requirements (Task 5)
- ✅ 4 offline scripts removed from root (Task 6)
- ✅ Pushed to GitHub (Task 7)

**Items NOT in scope (could not confirm as bugs):**
- Strike regex `\*` bug — not found; current code is correct
- `trades` missing `asset`/`strategy_variant` — not confirmed; both in CREATE TABLE
- `aiosqlite` removal — NOT removing; it IS used by bot_infra.py
- ETH/SOL/XRP/DOGE fake dashboard data — requires deeper audit of the live state API; out of scope for this cleanup

**Placeholder scan:** None found. All steps have exact code.

**Type consistency:** No new types introduced.
