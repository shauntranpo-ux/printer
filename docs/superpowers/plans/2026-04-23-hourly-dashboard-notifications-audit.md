# Hourly Dashboard Labeling, Notifications Retrofit, and Limit-Order Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label BTC/ETH hourly session markets on the dashboard, retrofit every Telegram notification with `[ASSET | session | ticker | phase?]` prefix, add a fill-verification alert for hourly fills, and audit + fix hourly limit-order price selection.

**Architecture:** Keep the bot-writes-state-file → dashboard-reads-state-file pattern. Extend per-asset entries in `bot_state.json` with `session_type`, `strategy_name`, `strike`, `phase`. Dashboard branches on `strategy_name` to render ETH (Mid/Dwell/Late phase panel) vs BTC (V3 mean-reversion panel). A small `_notify_ctx` helper in `bot.py` prefixes every `send_telegram` call. A new fill-verification notification fires inside `place_order` for hourly markets. Audit pass traces each hourly strategy's `entry_cents = yes_ask if side==YES else no_ask` invariant and `place_order`'s use of strategy target across retries.

**Tech Stack:** Python 3 (`bot.py`, `src/strategies/`), vanilla JS + HTML (`dashboard.html`), pytest.

---

## File Structure

**Modified:**
- `bot.py` — helpers (`_parse_strike_from_ticker`, `_notify_ctx`), `write_state_file` extension, retrofit of 7 `send_telegram` call sites, new fill-verification call site inside `place_order`, audit-driven fixes.
- `dashboard.html` — `_buildHourlyViewHtml` strategy branch, asset-card header.

**Created:**
- `tests/strategies/test_hourly_entry_price.py` — audit-level unit tests for Mid/Dwell/Late/BTCHourly entry_cents side-consistency.
- `tests/test_notify_ctx.py` — unit tests for `_parse_strike_from_ticker` and `_notify_ctx`.
- `tests/fixtures/bot_state_hourly.json` — mock state file for dashboard manual verification.

**Unchanged (no behavior change unless audit finds a bug):**
- `src/strategies/mid_window_strategy.py`, `dwell_window_strategy.py`, `late_window_strategy.py`, `btc_hourly_strategy.py` — only touched if audit reveals a bug; fixes documented in the design spec §5.2.

---

## Task 1: Ticker → strike parser

**Files:**
- Modify: `bot.py` (add helper near other market utilities, before `write_state_file`)
- Create: `tests/test_notify_ctx.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_notify_ctx.py`:

```python
"""Tests for ticker parsing and notification-context helpers in bot.py."""
import importlib.util
import sys
from pathlib import Path

# Import bot.py as a module without running its main.
_SPEC = importlib.util.spec_from_file_location(
    "kalshi_bot_module",
    Path(__file__).resolve().parents[1] / "bot.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
# Guard import-time side effects: the module reads env vars + config but doesn't
# auto-run; we only import the helper functions below.
sys.modules["kalshi_bot_module"] = _MOD
_SPEC.loader.exec_module(_MOD)


def test_parse_strike_btc_ticker():
    # Kalshi BTC hourly ticker format.
    assert _MOD._parse_strike_from_ticker("KXBTCD-26APR23-17:00-T94000") == 94000


def test_parse_strike_eth_ticker():
    assert _MOD._parse_strike_from_ticker("KXETHD-26APR23-17:00-T3500") == 3500


def test_parse_strike_15m_ticker():
    # 15m BTC ticker (no T-suffix strike, uses numeric suffix instead).
    assert _MOD._parse_strike_from_ticker("KXBTC15M-26APR231715-95000") == 95000


def test_parse_strike_no_match_returns_none():
    assert _MOD._parse_strike_from_ticker("GIBBERISH-TICKER") is None
    assert _MOD._parse_strike_from_ticker("") is None
    assert _MOD._parse_strike_from_ticker(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_notify_ctx.py -v`
Expected: 4 tests FAIL (AttributeError: module 'kalshi_bot_module' has no attribute '_parse_strike_from_ticker').

- [ ] **Step 3: Add `_parse_strike_from_ticker` to `bot.py`**

Place it just before `async def write_state_file(...)` (around `bot.py:3180`). Use Grep to find the exact line: `grep -n "async def write_state_file" bot.py`.

```python
import re

_STRIKE_RE_T_SUFFIX = re.compile(r"-T(\d+)$")           # hourly: ...-T94000
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d+)$")       # 15m: ...-95000

def _parse_strike_from_ticker(ticker: str | None) -> int | None:
    """Parse the strike price out of a Kalshi ticker.

    Hourly tickers use `-T<strike>` suffix; 15-minute tickers use `-<strike>`.
    Returns None if no pattern matches.
    """
    if not ticker:
        return None
    m = _STRIKE_RE_T_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    m = _STRIKE_RE_NUMERIC_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    return None
```

If `re` is already imported at the top of `bot.py`, skip the `import re` line (check with `grep -n "^import re" bot.py`).

- [ ] **Step 4: Run tests to verify pass**

Run: `py -m pytest tests/test_notify_ctx.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_notify_ctx.py
git commit -m "feat: add _parse_strike_from_ticker helper for dashboard strike labeling

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `_notify_ctx` helper for Telegram prefix

**Files:**
- Modify: `bot.py` (add helper near `send_telegram` at line ~639)
- Modify: `tests/test_notify_ctx.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_notify_ctx.py`:

```python
def test_notify_ctx_15m_no_phase():
    got = _MOD._notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0)
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_hourly_with_phase():
    got = _MOD._notify_ctx("ETH", "KXETHD-26APR23-17:00-T3500", duration_min=60.0, phase="Dwell")
    assert got == "[ETH | hourly | KXETHD-26APR23-17:00-T3500 | Dwell]"


def test_notify_ctx_hourly_no_phase():
    got = _MOD._notify_ctx("BTC", "KXBTCD-26APR23-17:00-T94000", duration_min=60.0)
    assert got == "[BTC | hourly | KXBTCD-26APR23-17:00-T94000]"


def test_notify_ctx_15m_ignores_phase():
    # 15m should never include phase, even if passed.
    got = _MOD._notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0, phase="Dwell")
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_threshold_boundary():
    # Boundary: 25.0 ⇒ 15m, 25.01 ⇒ hourly (matches _get_or_make_strategy rule).
    got_15m = _MOD._notify_ctx("ETH", "T", duration_min=25.0)
    got_hourly = _MOD._notify_ctx("ETH", "T", duration_min=25.01)
    assert "| 15m |" in got_15m
    assert "| hourly |" in got_hourly
```

- [ ] **Step 2: Run tests, verify fail**

Run: `py -m pytest tests/test_notify_ctx.py -v`
Expected: 5 new tests FAIL (module has no `_notify_ctx`).

- [ ] **Step 3: Add `_notify_ctx` and `_phase_for_eth` to `bot.py`**

Place immediately before `async def send_telegram(...)` (around `bot.py:639`):

```python
def _phase_for_eth(asset: str, elapsed_seconds: float) -> str | None:
    """Return ETH hourly window-phase label ('Mid'/'Dwell'/'Late') or None.

    BTC and all 15m markets return None (BTC V3 has no phase gate).
    """
    if asset != "ETH":
        return None
    m = elapsed_seconds / 60.0
    if 9 <= m <= 11:
        return "Mid"
    if 30 <= m <= 42:
        return "Dwell"
    if m >= 45:
        return "Late"
    return None


def _notify_ctx(
    asset: str,
    ticker: str,
    duration_min: float,
    phase: str | None = None,
) -> str:
    """Format a context prefix for Telegram notifications.

    Matches the strategy router rule at _get_or_make_strategy: >25 min = hourly.
    """
    session = "hourly" if duration_min > 25.0 else "15m"
    parts = [asset, session, ticker]
    if phase and session == "hourly":
        parts.append(phase)
    return f"[{' | '.join(parts)}]"
```

- [ ] **Step 4: Run tests, verify pass**

Run: `py -m pytest tests/test_notify_ctx.py -v`
Expected: 9 PASS total.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_notify_ctx.py
git commit -m "feat: add _notify_ctx helper for session-aware Telegram prefix

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Audit hourly strategy entry_cents side-consistency

**Files:**
- Create: `tests/strategies/test_hourly_entry_price.py`
- Modify (only if audit finds bugs): `src/strategies/mid_window_strategy.py`, `dwell_window_strategy.py`, `late_window_strategy.py`, `btc_hourly_strategy.py`

- [ ] **Step 1: Write audit tests**

Create `tests/strategies/test_hourly_entry_price.py`:

```python
"""Audit: entry_cents for hourly strategies must match the chosen side.

Invariant: if strategy picks side=YES, entry_cents == yes_ask.
           if strategy picks side=NO,  entry_cents == no_ask.

This catches side/ask mismatches that cause price-selection bugs in
limit-order placement.
"""
import time
from dataclasses import replace

import pytest

from strategies.features import MarketFeatures
from strategies.mid_window_strategy import MidWindowStrategy
from strategies.dwell_window_strategy import DwellWindowStrategy
from strategies.late_window_strategy import LateWindowStrategy
from strategies.btc_hourly_strategy import BTCHourlyStrategy


def _make_features(
    yes_ask: float = 80.0,
    no_ask: float = 21.0,
    elapsed_seconds: float = 600.0,
    seconds_left: float = 3000.0,
    current_price: float = 3510.0,
    strike: float = 3500.0,
) -> MarketFeatures:
    # Build a MarketFeatures with the minimum fields we care about.
    # Other fields get neutral defaults.
    return MarketFeatures(
        ticker="KXETHD-26APR23-17:00-T3500",
        current_price=current_price,
        strike=strike,
        elapsed_seconds=elapsed_seconds,
        seconds_left=seconds_left,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=yes_ask - 1.0,
        no_bid=no_ask - 1.0,
        spread_yes=1.0,
        spread_no=1.0,
        timestamp=time.time(),
        prices_list=[(time.time() - i * 60, 3500.0 + i) for i in range(15)],
        volumes_list=None,
        btc_prices_60m=[(time.time() - i * 60, 94000.0) for i in range(15)],
    )


def _assert_entry_matches_side(decision, features):
    """Core audit invariant."""
    if decision.action != "trade":
        return  # skip/hold paths don't need entry validation
    signals = decision.contributing_signals or {}
    entry = signals.get("entry_cents")
    if entry is None:
        return  # strategies not yet expanded — re-audit once expanded
    if decision.side == "yes":
        assert entry == features.yes_ask, (
            f"YES side must post at yes_ask={features.yes_ask}, got {entry}"
        )
    elif decision.side == "no":
        assert entry == features.no_ask, (
            f"NO side must post at no_ask={features.no_ask}, got {entry}"
        )


@pytest.mark.parametrize("cls,asset", [
    (MidWindowStrategy, "ETH"),
    (DwellWindowStrategy, "ETH"),
    (LateWindowStrategy, "ETH"),
])
def test_eth_hourly_yes_side_uses_yes_ask(cls, asset):
    strat = cls(asset, skip_config={}, stake_dollars=25.0, calibrator=None)
    features = _make_features(current_price=3510.0, strike=3500.0)  # above strike → YES
    decision = strat.decide(features)
    _assert_entry_matches_side(decision, features)


@pytest.mark.parametrize("cls,asset", [
    (MidWindowStrategy, "ETH"),
    (DwellWindowStrategy, "ETH"),
    (LateWindowStrategy, "ETH"),
])
def test_eth_hourly_no_side_uses_no_ask(cls, asset):
    strat = cls(asset, skip_config={}, stake_dollars=25.0, calibrator=None)
    features = _make_features(current_price=3490.0, strike=3500.0)  # below strike → NO
    decision = strat.decide(features)
    _assert_entry_matches_side(decision, features)


def test_btc_hourly_entry_matches_side():
    strat = BTCHourlyStrategy("BTC", skip_config={}, stake_dollars=25.0, calibrator=None)
    features = _make_features(current_price=94100.0, strike=94000.0,
                              yes_ask=55.0, no_ask=46.0)
    decision = strat.decide(features)
    _assert_entry_matches_side(decision, features)
```

- [ ] **Step 2: Run tests, observe results**

Run: `py -m pytest tests/strategies/test_hourly_entry_price.py -v`

Expected: tests will either pass (invariant holds) or fail (bug found). Record exact failures.

- [ ] **Step 3: If any test fails, fix the bug**

For each failure, open the relevant strategy file and fix the `entry_cents` selection. The canonical form is already in `src/strategies/mid_window_strategy.py:170`:

```python
entry_cents = features.yes_ask if eth_itm else features.no_ask
```

If a strategy returns `action="trade"` with `side="no"` but computes `entry_cents = yes_ask` (or vice versa), swap the branch. Add a one-line code comment recording the invariant: `# YES side → yes_ask; NO side → no_ask (audit invariant 2026-04-23).`

If a strategy returns `action="trade"` without putting `entry_cents` in `contributing_signals`, add it: `decision.contributing_signals["entry_cents"] = features.yes_ask if side == "yes" else features.no_ask`.

- [ ] **Step 4: Re-run tests, verify all pass**

Run: `py -m pytest tests/strategies/test_hourly_entry_price.py -v`
Expected: all PASS.

- [ ] **Step 5: Record findings in design spec**

Edit `docs/superpowers/specs/2026-04-23-hourly-dashboard-notifications-audit-design.md` §5.2, replace `TBD` with findings:

```markdown
### 5.2 Findings

- [File:line]: [description]. Fix: [what changed]. Invariant restored: entry_cents matches decision.side.

(Or: "No bugs found — invariant holds across Mid/Dwell/Late/BTCHourly.")
```

- [ ] **Step 6: Commit**

```bash
git add tests/strategies/test_hourly_entry_price.py docs/superpowers/specs/2026-04-23-hourly-dashboard-notifications-audit-design.md
# plus any strategy files modified
git add src/strategies/*.py 2>/dev/null || true
git commit -m "test: audit hourly strategies' entry_cents side-consistency

$(if git diff HEAD~0 --name-only | grep -q src/strategies; then echo 'Fixed: <summarize fixes>'; else echo 'No bugs found.'; fi)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

(If no strategy files changed, simplify the commit message body.)

---

## Task 4: Audit `place_order` — capture market ask at post time

**Files:**
- Modify: `bot.py` around the `place_order` function (line ~2560)

- [ ] **Step 1: Read `place_order` to understand current state**

Run: `grep -n "def place_order\|price_this_attempt\|best_yes_ask\|best_no_ask" bot.py | head -20`

Goal: identify where `price_this_attempt` is set inside the retry loop, where `best_yes_ask`/`best_no_ask` come from (once before the loop or refreshed per attempt), and where we know the fill confirmed.

- [ ] **Step 2: Confirm invariant — `price_this_attempt` equals strategy target across retries**

Walk the retry loop by hand. Invariant: `price_this_attempt` is set from `entry_price_cents` (the strategy's chosen target) at loop start and NEVER re-assigned from a fresh orderbook snapshot mid-loop.

If `price_this_attempt` is re-assigned from a snapshot (e.g., `price_this_attempt = best_yes_ask` inside the retry body), that is a **bug**. Record it, then fix by removing the re-assignment or routing it through a named `_refresh_attempt_price()` helper that the test can gate.

- [ ] **Step 3: Capture `market_ask_at_post` before first post**

Immediately before the first HTTP post in `place_order`, add:

```python
# Capture the market ask at post time for fill-verification telemetry.
# Must match the side we're trading: YES side → yes_ask, NO side → no_ask.
market_ask_at_post = (
    orderbook.get("best_yes_ask") if side == "yes" else orderbook.get("best_no_ask")
)
```

Where `orderbook` is the dict already built by the orderbook fetch earlier in the function. If the variable is named differently, use that name. Find with: `grep -n "best_yes_ask\|best_no_ask" bot.py | head -5` and read 20 lines of surrounding context.

- [ ] **Step 4: Commit** (no behavior change yet — this just captures the variable; the notification in Task 5 uses it)

```bash
git add bot.py
git commit -m "feat: capture market_ask_at_post in place_order for fill telemetry

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Add fill-verification notification (hourly only)

**Files:**
- Modify: `bot.py` (inside `place_order`, immediately after fill confirms at line ~3001)

- [ ] **Step 1: Find the fill-confirmation block**

Run: `grep -n "_fill_yes_price\|fill_confirmed" bot.py | head -10`

Locate the block (around line 3001) where the fill is confirmed and `_fill_yes_price` is set.

- [ ] **Step 2: Add fill-verification notification**

Immediately after the fill is confirmed and BEFORE the existing `send_telegram` at line ~3792 (which notifies the fill), insert:

```python
# Fill-verification alert (hourly markets only — lets us spot price-selection bugs in flight).
_is_hourly = (elapsed_seconds + secs_left) / 60.0 > 25.0
if _is_hourly and market_ask_at_post is not None and _fill_yes_price is not None:
    _target_c = int(round(entry_price_cents))
    _ask_c = int(round(market_ask_at_post))
    _posted_c = int(round(price_this_attempt))
    _fill_c = int(round(_fill_yes_price))
    _slip_target = _fill_c - _target_c
    _slip_market = _fill_c - _ask_c
    _warn = "⚠️ " if abs(_slip_target) > 3 else "🎯 "
    _ctx = _notify_ctx(
        asset, ticker, (elapsed_seconds + secs_left) / 60.0,
        _phase_for_eth(asset, elapsed_seconds),
    )
    await send_telegram(
        f"{_warn}<b>{_ctx} FILL VERIFICATION</b>\n"
        f"Target:     <b>{_target_c}¢</b>\n"
        f"Market ask: {_ask_c}¢\n"
        f"Posted:     {_posted_c}¢\n"
        f"Filled:     <b>{_fill_c}¢</b>\n"
        f"Slippage:   {_slip_target:+d}¢ vs target  |  {_slip_market:+d}¢ vs market"
    )
```

Ensure `asset`, `ticker`, `elapsed_seconds`, `secs_left`, `entry_price_cents`, `price_this_attempt`, `market_ask_at_post`, `_fill_yes_price`, and `side` are all in scope at that point. Use Grep to confirm each variable's origin; add it to `place_order`'s parameter list if not already present.

- [ ] **Step 3: Manually verify by reading the surrounding code**

Re-read the full `place_order` function to confirm the new block sits after fill confirmation and before the existing fill-confirmation Telegram message. The new message runs first (verification), then the existing message (entry announcement).

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: fill-verification Telegram alert for hourly markets

Shows target / market ask / posted / filled / slippage after every hourly
fill. Warns with ⚠️ when slippage vs target > 3¢.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Retrofit existing `send_telegram` calls with `_notify_ctx`

**Files:**
- Modify: `bot.py` — each of the 7 target call sites

Each step below is one call site. The pattern: build `_ctx = _notify_ctx(asset, ticker, duration_min, phase)` (phase optional), then prefix the existing message with `f"{_ctx} "`.

`duration_min` comes from `(elapsed_seconds + secs_left) / 60.0` if both are in scope; otherwise use `market_duration_min` or re-derive from the `market` dict: `(market["close_ts"] - market["open_ts"]) / 60.0` (grep for existing usages).

For ETH hourly, compute `phase` as in Task 5 Step 2. For BTC and all 15m, pass `phase=None`.

Both helpers `_phase_for_eth` and `_notify_ctx` were added in Task 2 Step 3 — use them directly.

- [ ] **Step 1: Limit placed (`bot.py:2652`)**

Before:
```python
asyncio.create_task(send_telegram(
    f"📋 <b>[{asset}] LIMIT ORDER PLACED</b>\n"
    ...
))
```

After:
```python
_ctx = _notify_ctx(asset, ticker, (elapsed_seconds + secs_left) / 60.0,
                   _phase_for_eth(asset, elapsed_seconds))
asyncio.create_task(send_telegram(
    f"📋 <b>{_ctx} LIMIT ORDER PLACED</b>\n"
    ...
))
```

- [ ] **Step 2: Order failed (non-retryable, `bot.py:2907`)**

Apply same pattern:
```python
_ctx = _notify_ctx(asset, ticker, (elapsed_seconds + secs_left) / 60.0,
                   _phase_for_eth(asset, elapsed_seconds))
await send_telegram(
    f"{_ctx} <b>ORDER FAILED</b>  —  {err_code}\n"
    f"{side.upper()}  {contracts}x @ {price_this_attempt}c"
)
```

- [ ] **Step 3: Order not filled (no liquidity, `bot.py:3035`)**

```python
_ctx = _notify_ctx(asset, ticker, (elapsed_seconds + secs_left) / 60.0,
                   _phase_for_eth(asset, elapsed_seconds))
await send_telegram(
    f"⚠️ <b>{_ctx} ORDER NOT FILLED</b>  —  no liquidity\n"
    f"{side.upper()}  {contracts}x @ {entry_price_cents}¢"
)
```

- [ ] **Step 4: Fill confirmed / reversal (`bot.py:3792`)**

Existing message includes `_strat_tag = "🔄 REVERSAL" if _is_reversal else "LIMIT ORDER FILLED"`:

```python
_ctx = _notify_ctx(asset, ticker, (elapsed_seconds + secs_left) / 60.0,
                   _phase_for_eth(asset, elapsed_seconds))
await send_telegram(
    f"{mode_icon} <b>{_ctx} {_strat_tag}</b>  —  {_time_str}\n"
    ...
)
```

Drop the `[{asset}]` from the original (the new `_ctx` already contains it).

- [ ] **Step 5: Consecutive-loss pause (`bot.py:3915`)**

No `ticker`/`elapsed_seconds` in scope at this site. Use the `pos` dict (closed position) to get `ticker` and `(pos.get("entry_ts"), pos.get("close_ts"))` for duration estimate:

```python
_dur_min = (pos.get("close_ts", time.time()) - pos.get("entry_ts", time.time())) / 60.0
# Approximate: if held ≥25 min, call it hourly; otherwise 15m.
_ctx = _notify_ctx(asset, pos.get("ticker", "?"), _dur_min,
                   _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)))
await send_telegram(
    f"⚠️ <b>{_ctx} {_consecutive_losses} consecutive losses</b> — pausing 15 min.\n"
    f"Resumes at {_resume_str}"
)
```

If `pos` lacks `close_ts` or `elapsed_at_entry`, add them at position-close time in the nearest upstream call (grep for `pos["entry_ts"]` and add `pos["close_ts"] = time.time()` nearby).

- [ ] **Step 6: Win / loss close (`bot.py:3927`)**

`pos` is in scope; `ticker = pos["ticker"]`. Use same pattern as Step 5.

```python
_dur_min = (pos.get("close_ts", time.time()) - pos.get("entry_ts", time.time())) / 60.0
_ctx = _notify_ctx(asset, pos.get("ticker", "?"), _dur_min,
                   _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)))
await send_telegram(
    f"{result_icon} <b>{_ctx} {'WIN' if outcome == 'win' else 'LOSS'}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
    f"{mode_icon}  {pos['side'].upper()}  {pos['contracts']} contracts  |  held {_dur_str}\n"
    f"Entry: {pos['entry_price_cents']}¢  →  Expiry: {exit_price}¢"
)
```

Drop the trailing `| <code>{ticker}</code>` since `_ctx` already carries the ticker.

- [ ] **Step 7: Manually verify message shape by grepping all `send_telegram` calls**

Run: `grep -n "send_telegram" bot.py | head -20`
Read each match's surrounding 4 lines; confirm every trade-context site has `_ctx` prefix.

Call sites 4583 (preflight) and 4678 (startup) and 3131 (demo DLL) stay unchanged by design.

- [ ] **Step 8: Commit**

```bash
git add bot.py
git commit -m "feat: retrofit trade-related Telegram notifications with [ASSET | session | ticker | phase?] prefix

Every limit-placed, fill, failed, not-filled, win/loss, and consecutive-loss
notification now carries the session-market context. Pre-flight, startup,
and DLL notifications unchanged (no market context applicable).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Extend `write_state_file` with hourly fields

**Files:**
- Modify: `bot.py` — `write_state_file` and the per-asset state builders (lines ~3185-3280)

- [ ] **Step 1: Locate the per-asset state builders**

Run: `grep -n "def write_state_file\|_asset_states\|\"signals\":" bot.py | head -20`

You'll find:
- Line ~3229: loop building non-BTC asset entries from `_asset_states`.
- Line ~3261: BTC-specific branch.

- [ ] **Step 2: Compute hourly fields once**

At the top of `write_state_file`, compute for the primary market:

```python
_duration_min = (
    (market.get("close_ts", 0) - market.get("open_ts", 0)) / 60.0
    if market else 0.0
)
_session_type = "hourly" if _duration_min > 25.0 else "15m"
_strike = _parse_strike_from_ticker(market.get("ticker") if market else None) or (
    market.get("strike_price") if market else None
)
```

- [ ] **Step 3: Pick strategy name per asset**

Add a small helper near `_get_or_make_strategy` (line ~1833):

```python
def _strategy_name_for(asset: str, duration_min: float) -> str:
    """Human-readable strategy name for dashboard."""
    is_hourly = duration_min > 25.0
    if asset == "ETH" and is_hourly:
        return "ETHHourlyCombined"
    if asset == "BTC" and is_hourly:
        return "BTCHourly V3"
    if is_hourly:
        return f"{asset}Hourly"
    return f"{asset}15m"
```

- [ ] **Step 4: Extend the non-BTC asset-entry dict (line ~3237)**

Inside the `for _a, _st in _asset_states.items():` loop, expand the dict pushed into `assets`:

```python
_a_ev = _st.get("ev", {})
_a_market = _st.get("market", {})
_a_ticker = _a_market.get("ticker", "")
_a_duration_min = (
    (_a_market.get("close_ts", 0) - _a_market.get("open_ts", 0)) / 60.0
    if _a_market else 0.0
)
_a_session_type = "hourly" if _a_duration_min > 25.0 else "15m"
_a_strategy_name = _strategy_name_for(_a, _a_duration_min)
_a_strike = _parse_strike_from_ticker(_a_ticker) or _a_market.get("strike_price")
_a_elapsed_sec = _st.get("elapsed_seconds", 0.0)
_a_phase = _phase_for_eth(_a, _a_elapsed_sec) if _a_session_type == "hourly" else None

assets.append({
    "asset":         _a,
    "market_ticker": _a_ticker,
    "market_title":  _a_market.get("title", ""),
    "session_type":  _a_session_type,          # NEW
    "strategy_name": _a_strategy_name,         # NEW
    "strike":        _a_strike,                # NEW
    "phase":         _a_phase,                 # NEW (None for non-ETH/non-hourly)
    "phase_label":   _a_phase,                 # NEW (dashboard-friendly)
    "signals":       _a_ev.get("signals", {}),
    ... existing fields ...
})
```

Preserve all existing keys; only add the five new keys (`session_type`, `strategy_name`, `strike`, `phase`, `phase_label`). If the existing code already has overlapping keys, keep the existing line and add only the missing ones.

- [ ] **Step 5: Extend BTC asset-entry dict (line ~3261)**

Apply the same pattern to the BTC branch. BTC never has `phase` (V3 doesn't use phase gates), so pass `phase=None` explicitly:

```python
_btc_duration_min = (
    (market.get("close_ts", 0) - market.get("open_ts", 0)) / 60.0
    if market else 0.0
)
_btc_session_type = "hourly" if _btc_duration_min > 25.0 else "15m"
_btc_strategy_name = _strategy_name_for("BTC", _btc_duration_min)
_btc_strike = _parse_strike_from_ticker(market.get("ticker", "")) or (
    market.get("strike_price") if market else None
)

assets.append({
    "asset":         "BTC",
    "market_ticker": market.get("ticker", "") if market else "",
    "market_title":  market.get("title",  "") if market else "",
    "session_type":  _btc_session_type,     # NEW
    "strategy_name": _btc_strategy_name,    # NEW
    "strike":        _btc_strike,           # NEW
    "phase":         None,                  # NEW (BTC V3 has no phase)
    "phase_label":   None,                  # NEW
    "signals":       _btc_ev.get("signals", {}),
    ... existing fields ...
})
```

- [ ] **Step 6: Run the bot briefly in paper mode; sanity-check the state file**

Start the bot (paper mode) and let it run one cycle:

```bash
py bot.py &
BOT_PID=$!
sleep 20
kill $BOT_PID
py -c "import json; d=json.load(open('bot_state.json')); import sys; [print(a.get('asset'), a.get('session_type'), a.get('strategy_name'), a.get('strike'), a.get('phase')) for a in d.get('assets', [])]"
```

Expected output (rows): `ETH hourly ETHHourlyCombined 3500 Dwell` (or None for phase if between windows), `BTC hourly BTCHourly V3 94000 None`.

If any field is missing/wrong, fix and re-run.

- [ ] **Step 7: Commit**

```bash
git add bot.py
git commit -m "feat: extend bot_state.json per-asset entries with session_type, strategy_name, strike, phase

Dashboard uses these to pick the right signal panel (ETH Mid/Dwell/Late vs
BTC V3 mean-reversion) and display the current hourly contract's ticker+strike.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Dashboard — update asset-card header

**Files:**
- Modify: `dashboard.html` (around line 779 — asset-card header in `renderMarketView`)

- [ ] **Step 1: Find the current header**

Run: `grep -n "mv-asset-name" dashboard.html`

Located at ~line 779:
```javascript
<div class="mv-asset-name">${sym} Hourly</div>
```

- [ ] **Step 2: Replace with ticker+strike+strategy header**

Update to pull from `a` (the asset-state object):

```javascript
<div class="mv-asset-name">
  ${sym} ${a.session_type === 'hourly' ? 'Hourly' : '15m'}
  ${a.market_ticker ? `<span style="color:var(--ink-3);font-size:11px;font-weight:400;">| ${a.market_ticker}</span>` : ''}
  ${a.strike != null ? `<span style="color:var(--ink-3);font-size:11px;font-weight:400;">| Strike $${a.strike.toLocaleString()}</span>` : ''}
  ${a.strategy_name ? `<span style="color:var(--accent);font-size:11px;font-weight:600;margin-left:8px;">${a.strategy_name}</span>` : ''}
</div>
```

- [ ] **Step 3: Manual visual test**

Create `tests/fixtures/bot_state_hourly.json`:

```json
{
  "ts": "2026-04-23T17:05:00+00:00",
  "mode": "paper",
  "assets": [
    {
      "asset": "ETH",
      "market_ticker": "KXETHD-26APR23-17:00-T3500",
      "market_title": "ETH above $3,500 at 5pm?",
      "session_type": "hourly",
      "strategy_name": "ETHHourlyCombined",
      "strike": 3500,
      "phase": "Dwell",
      "phase_label": "Dwell",
      "signals": {
        "elapsed_min": 32.5,
        "eth_cross": 0,
        "btc_cross": 0,
        "eth_dist_pct": 0.42,
        "eth_itm": true,
        "btc_itm": true,
        "dwell_itm": 0.85,
        "streak_frac": 0.68,
        "entry_cents": 82
      },
      "secs_left": 1650
    },
    {
      "asset": "BTC",
      "market_ticker": "KXBTCD-26APR23-17:00-T94000",
      "market_title": "BTC above $94,000 at 5pm?",
      "session_type": "hourly",
      "strategy_name": "BTCHourly V3",
      "strike": 94000,
      "phase": null,
      "phase_label": null,
      "signals": {
        "session": "other",
        "vwap_z": 1.42,
        "rsi": 28,
        "bollinger": "below",
        "momentum_reversal": "fade_down",
        "vwap_adj": -0.04,
        "rsi_adj": -0.02,
        "bb_adj": -0.02,
        "mom_adj": 0.0,
        "total_adj_before_taper": -0.08,
        "final_p_yes": 0.47,
        "baseline_p_above": 0.55
      },
      "secs_left": 1650
    }
  ]
}
```

Temporarily replace `bot_state.json` (backup first):
```bash
cp bot_state.json bot_state.json.bak
cp tests/fixtures/bot_state_hourly.json bot_state.json
```

Start the server:
```bash
py server.py &
SERVER_PID=$!
sleep 3
```

Open `http://localhost:<port>` (or check `server.py` for port). Verify ETH and BTC cards show: `ETH Hourly | KXETHD-... | Strike $3,500 | ETHHourlyCombined`.

Restore:
```bash
kill $SERVER_PID
mv bot_state.json.bak bot_state.json
```

- [ ] **Step 4: Commit**

```bash
git add dashboard.html tests/fixtures/bot_state_hourly.json
git commit -m "feat: dashboard asset-card header shows ticker, strike, strategy name

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 9: Dashboard — BTC V3 signal panel

**Files:**
- Modify: `dashboard.html` — `_buildHourlyViewHtml(a)` function (line 1285)

- [ ] **Step 1: Branch `_buildHourlyViewHtml` on strategy_name**

Before (line 1285):
```javascript
function _buildHourlyViewHtml(a) {
  const s = a.signals || {};
  const elMin = ...;
  ...
}
```

After:
```javascript
function _buildHourlyViewHtml(a) {
  if (a.strategy_name === 'BTCHourly V3') return _buildBTCHourlyViewHtml(a);
  // Default: ETH hourly Mid/Dwell/Late panel (existing).
  const s = a.signals || {};
  ... existing ...
}

function _buildBTCHourlyViewHtml(a) {
  const s = a.signals || {};
  const session = s.session || 'unknown';
  const sessionDisplay = session === 'asian'
    ? '<span style="color:var(--down);font-weight:700;">Asian — SKIPPED</span>'
    : '<span style="color:var(--up);font-weight:700;">Active</span>';
  const rows = [
    ['Session',    sessionDisplay],
    ['VWAP z',     s.vwap_z != null ? (s.vwap_z >= 0 ? '+' : '') + s.vwap_z.toFixed(2) : '—'],
    ['RSI',        s.rsi != null ? s.rsi.toFixed(0) : '—'],
    ['Bollinger',  s.bollinger || '—'],
    ['Momentum',   s.momentum_reversal || '—'],
    ['Total adj',  s.total_adj_before_taper != null ? (s.total_adj_before_taper >= 0 ? '+' : '') + s.total_adj_before_taper.toFixed(3) : '—'],
    ['p_yes',      s.final_p_yes != null ? s.final_p_yes.toFixed(2) + (s.baseline_p_above != null ? ` <span style="color:var(--ink-3)">(baseline ${s.baseline_p_above.toFixed(2)})</span>` : '') : '—'],
    ['Entry',      s.entry_cents != null ? s.entry_cents.toFixed(0) + '¢' : '—'],
  ].filter(([,v]) => v !== '—');

  if (!rows.length) {
    return '<div style="color:var(--ink-3);font-size:12px;padding:8px 0;">No BTC V3 data — strategy skipped or mid-signal-build.</div>';
  }
  return rows.map(([k,v]) => `<div class="cf-row"><div class="cf-dot"></div><span class="cf-name">${k}</span><span class="cf-val">${v}</span></div>`).join('');
}
```

- [ ] **Step 2: Visual test using the same fixture from Task 8**

Re-run Task 8 Step 3 (swap `bot_state.json` for the fixture). Confirm:
- ETH card body: Phase / Elapsed / crossings / ITM / Dwell / Streak / Entry rows (unchanged).
- BTC card body: Session (Active) / VWAP z (+1.42) / RSI (28) / Bollinger (below) / Momentum (fade_down) / Total adj (−0.08) / p_yes (0.47 baseline 0.55) / no Entry row (since fixture didn't set entry_cents).

- [ ] **Step 3: Test Asian session branch**

Edit the fixture, change `"session": "other"` → `"session": "asian"`, reload. Confirm Session row shows red "Asian — SKIPPED".

Restore fixture to `"other"`.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: dashboard BTC hourly panel renders V3 mean-reversion signals

Branches _buildHourlyViewHtml on strategy_name. BTC V3 shows Session,
VWAP z, RSI, Bollinger, Momentum, Total adj, p_yes+baseline, Entry.
ETH path unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 10: End-to-end paper integration test

**Files:**
- None created; this is a live smoke test.

- [ ] **Step 1: Ensure paper mode and confirm config**

Run: `py -c "import json; print(json.load(open('config.json')))"`

Expected: `"mode": "paper"`, `"bot_enabled": true`.

If not paper, edit `config.json` to set `"mode": "paper"` and `"bot_enabled": true`. Commit nothing (this is temporary).

- [ ] **Step 2: Ensure Telegram is wired (optional but recommended)**

Run: `py -c "import os; print('BOT:', bool(os.environ.get('TELEGRAM_BOT_TOKEN'))); print('CHAT:', bool(os.environ.get('TELEGRAM_CHAT_ID')))"`

If both True, notifications will fire. If not, messages are silently dropped (the test still passes but you won't see the alerts).

- [ ] **Step 3: Run bot for one full hourly window (≥70 min)**

```bash
py bot.py
```

Watch for log lines:
- `Strategy initialized: ETH_hourly (hourly, stake=$...)`
- `Strategy initialized: BTC_hourly (hourly, stake=$...)`

After ~10 min, check `bot_state.json`:
```bash
py -c "import json; d=json.load(open('bot_state.json')); [print(a['asset'], a.get('session_type'), a.get('strategy_name'), a.get('strike'), a.get('phase'), list(a.get('signals',{}).keys())[:5]) for a in d.get('assets',[])]"
```

Expected:
- `ETH hourly ETHHourlyCombined <strike> Mid/Dwell/Late/None ['elapsed_min', ...]`
- `BTC hourly BTCHourly V3 <strike> None ['session', 'vwap_z', ...]`

- [ ] **Step 4: Verify dashboard**

With bot still running, open the dashboard (`py server.py` in another shell, browse to `http://localhost:<port>`). Confirm:
- Both cards show ticker + strike + strategy name.
- ETH card renders Mid/Dwell/Late phase panel.
- BTC card renders V3 signal panel.
- Values update as the window progresses.

- [ ] **Step 5: If an entry fires, verify notifications**

When a paper entry fires, check Telegram. Expected messages, in order:
1. `📋 [ETH | hourly | <ticker> | Dwell] LIMIT ORDER PLACED ...`
2. `🎯 [ETH | hourly | <ticker> | Dwell] FILL VERIFICATION` with 5 numeric rows.
3. `<mode_icon> [ETH | hourly | <ticker> | Dwell] LIMIT ORDER FILLED ...`
4. On close: `<result_icon> [ETH | hourly | <ticker> | Dwell] WIN/LOSS ...`

If any message lacks the `[... | ...]` prefix, grep `bot.py` for that call site and retrofit (missed in Task 6).

- [ ] **Step 6: Stop bot; commit nothing (integration test)**

```bash
# Ctrl-C the bot
```

Record any findings from this integration test in the design spec §5.2 if new bugs surface.

---

## Self-review checklist

**Spec coverage** (against `docs/superpowers/specs/2026-04-23-hourly-dashboard-notifications-audit-design.md`):

- §3 data flow / state contract → Task 7 ✓
- §4.1 card header → Task 8 ✓
- §4.2 ETH hourly body (unchanged) → covered in Task 9 branch ✓
- §4.3 BTC hourly body → Task 9 ✓
- §4.4 sync guarantee → Task 7 writes the same fields the dashboard reads ✓
- §5 limit-order + price-selection audit → Tasks 3 + 4 ✓
- §6.1 `_notify_ctx` helper → Task 2 ✓
- §6.2 retrofit of existing `send_telegram` → Task 6 ✓
- §6.3 new fill-verification notification → Task 5 ✓
- §8.1 dashboard rendering test → Task 8 Step 3, Task 9 Step 2 ✓
- §8.2 integration → Task 10 ✓
- §8.3 audit harness → Task 3 ✓

**Placeholder scan:** no "TBD" in task bodies; §5.2 of the spec still has a TBD (filled at execution time — Task 3 Step 5). This is intentional: the audit produces findings the plan cannot predict.

**Type consistency:** `session_type` (string) / `strategy_name` (string) / `strike` (int|null) / `phase` (string|null) used consistently across Tasks 7, 8, 9. Helper `_phase_for_eth(asset, elapsed_seconds)` defined in Task 6 Step 1, reused in Tasks 5 (originally inlined, replaced), 6 (all steps), 7 (Step 4). `_notify_ctx` signature stable.

---

## Execution note

This plan assumes you execute tasks in numeric order. Tasks 1→2 are parallel-safe (both add helpers to `bot.py`). Tasks 3 and 4 can run in parallel. Tasks 5 depends on 2+4. Task 6 depends on 2. Task 7 depends on 1+2. Tasks 8 and 9 depend on 7. Task 10 depends on all prior.
