# Daily Stats & Telegram Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-trade Telegram noise with a clean daily summary push; add a terminal stats script.

**Architecture:** New `bot_stats.py` module holds all query + format logic. `scripts/stats_report.py` is a thin CLI wrapper. `bot_loops.py` fires the daily Telegram push on the first loop tick after UTC midnight. Seven per-trade `send_telegram` calls are deleted across `bot_market.py`, `bot_loops.py`, `bot_risk.py`.

**Tech Stack:** Python stdlib (`sqlite3`, `datetime`, `re`). No new dependencies.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `bot_stats.py` | DB queries + Telegram/terminal formatting |
| Create | `scripts/stats_report.py` | CLI wrapper — prints stats to stdout |
| Create | `tests/test_bot_stats.py` | Unit tests for query + format logic |
| Modify | `bot_loops.py` | Add midnight trigger; remove 2 per-trade `send_telegram` calls |
| Modify | `bot_market.py` | Remove 3 per-trade `send_telegram` calls |
| Modify | `bot_risk.py` | Remove 2 per-trade `send_telegram` calls |

---

## Task 1: bot_stats.py — query_stats

**Files:**
- Create: `bot_stats.py`
- Create: `tests/test_bot_stats.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bot_stats.py
import sqlite3
import pytest
import bot_stats


def _make_db(trades: list[dict]) -> str:
    """Create an in-memory SQLite DB with the trades schema. Returns ':memory:' path."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT, strategy_variant TEXT, asset TEXT,
            outcome TEXT, pnl_dollars REAL
        )
    """)
    for t in trades:
        conn.execute(
            "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
            (t["ts"], t["strategy_variant"], t["asset"], t["outcome"], t["pnl_dollars"]),
        )
    conn.commit()
    # bot_stats.query_stats expects a file path — monkey-patch sqlite3.connect to return our conn
    return conn


def test_query_stats_today_aggregates(monkeypatch):
    """Today's wins and losses aggregate correctly by strategy+asset."""
    conn = _make_db([
        {"ts": "2026-05-07T10:00:00", "strategy_variant": "strategy1", "asset": "BTC", "outcome": "win",  "pnl_dollars": 10.0},
        {"ts": "2026-05-07T11:00:00", "strategy_variant": "strategy1", "asset": "BTC", "outcome": "loss", "pnl_dollars": -5.0},
        {"ts": "2026-05-07T12:00:00", "strategy_variant": "strategy2", "asset": "ETH", "outcome": "win",  "pnl_dollars": 8.0},
        {"ts": "2026-05-06T10:00:00", "strategy_variant": "strategy1", "asset": "BTC", "outcome": "win",  "pnl_dollars": 99.0},  # yesterday
    ])
    monkeypatch.setattr(sqlite3, "connect", lambda _path: conn)

    stats = bot_stats.query_stats(":memory:", today_date="2026-05-07")

    assert stats["today_trades"] == 3
    assert stats["today_wins"]   == 2
    assert stats["today_losses"] == 1
    assert abs(stats["today_pnl"] - 13.0) < 0.01
    assert ("strategy1", "BTC") in stats["by_strategy_asset"]
    assert stats["by_strategy_asset"][("strategy1", "BTC")]["wins"]   == 1
    assert stats["by_strategy_asset"][("strategy1", "BTC")]["losses"] == 1


def test_query_stats_alltime(monkeypatch):
    """All-time totals include all outcomes, not just today."""
    conn = _make_db([
        {"ts": "2026-04-01T10:00:00", "strategy_variant": "strategy2", "asset": "BTC", "outcome": "win",  "pnl_dollars": 50.0},
        {"ts": "2026-04-01T11:00:00", "strategy_variant": "strategy2", "asset": "BTC", "outcome": "loss", "pnl_dollars": -20.0},
    ])
    monkeypatch.setattr(sqlite3, "connect", lambda _path: conn)

    stats = bot_stats.query_stats(":memory:", today_date="2026-05-07")

    assert stats["alltime_trades"] == 2
    assert stats["alltime_wins"]   == 1
    assert abs(stats["alltime_pnl"] - 30.0) < 0.01


def test_query_stats_no_trades(monkeypatch):
    """Empty DB returns all zeros, no crash."""
    conn = _make_db([])
    monkeypatch.setattr(sqlite3, "connect", lambda _path: conn)

    stats = bot_stats.query_stats(":memory:", today_date="2026-05-07")

    assert stats["today_trades"]   == 0
    assert stats["alltime_trades"] == 0
    assert stats["last_trade_ts"]  is None


def test_query_stats_db_unavailable(monkeypatch):
    """DB error returns zeroed dict, does not raise."""
    monkeypatch.setattr(sqlite3, "connect", lambda _path: (_ for _ in ()).throw(Exception("no db")))

    stats = bot_stats.query_stats("/nonexistent/path.db", today_date="2026-05-07")

    assert stats["today_trades"] == 0
```

- [ ] **Step 2: Run to confirm it fails**

```
py -m pytest tests/test_bot_stats.py -v
```

Expected: `ModuleNotFoundError: No module named 'bot_stats'`

- [ ] **Step 3: Create bot_stats.py with query_stats**

```python
# bot_stats.py
"""
bot_stats.py — Trade statistics queries and formatting.

query_stats(db_path, today_date) -> dict
format_telegram(stats) -> str   (HTML for Telegram)
format_terminal(stats) -> str   (plain text)
"""
import datetime
import re
import sqlite3
from collections import defaultdict

_STRATEGY_LABELS = {
    "strategy1": "S1 · EMA Momentum",
    "strategy2": "S2 · Contract Velocity",
}
_SEP = "─" * 27


def query_stats(db_path: str, today_date: str | None = None) -> dict:
    """
    Query trade statistics from the SQLite DB.

    Args:
        db_path:    Path to kalshi_bot.db.
        today_date: UTC date string 'YYYY-MM-DD'. Defaults to today.

    Returns dict with keys:
        date, today_trades, today_wins, today_losses, today_pnl,
        alltime_trades, alltime_wins, alltime_pnl,
        by_strategy_asset: {(strategy_variant, asset): {"wins": N, "losses": N, "pnl": X}},
        last_trade_ts, consecutive_losses, mode.
    """
    today = today_date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    by_sa: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    today_trades = today_wins = today_losses = 0
    today_pnl = alltime_trades = alltime_wins = 0
    alltime_pnl = 0.0
    last_trade_ts = None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT strategy_variant, asset, outcome,
                   COUNT(1) as n, SUM(pnl_dollars) as pnl
            FROM trades
            WHERE DATE(ts) = ? AND outcome IN ('win', 'loss')
            GROUP BY strategy_variant, asset, outcome
            """,
            (today,),
        ).fetchall()
        for r in rows:
            key = (r["strategy_variant"] or "unknown", r["asset"] or "?")
            n   = r["n"] or 0
            p   = r["pnl"] or 0.0
            if r["outcome"] == "win":
                by_sa[key]["wins"]   += n
                today_wins           += n
            else:
                by_sa[key]["losses"] += n
                today_losses         += n
            by_sa[key]["pnl"] += p
            today_trades      += n
            today_pnl         += p

        row = conn.execute(
            """
            SELECT COUNT(1) as n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                   SUM(pnl_dollars) as pnl
            FROM trades WHERE outcome IN ('win', 'loss')
            """
        ).fetchone()
        if row:
            alltime_trades = row["n"]    or 0
            alltime_wins   = row["wins"] or 0
            alltime_pnl    = row["pnl"]  or 0.0

        lt = conn.execute(
            "SELECT ts FROM trades WHERE outcome IN ('win','loss') ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if lt:
            last_trade_ts = lt["ts"]

        conn.close()
    except Exception:
        pass

    return {
        "date":               today,
        "today_trades":       today_trades,
        "today_wins":         today_wins,
        "today_losses":       today_losses,
        "today_pnl":          round(today_pnl, 2),
        "alltime_trades":     alltime_trades,
        "alltime_wins":       alltime_wins,
        "alltime_pnl":        round(alltime_pnl, 2),
        "by_strategy_asset":  dict(by_sa),
        "last_trade_ts":      last_trade_ts,
        "consecutive_losses": 0,
        "mode":               "PAPER",
    }
```

- [ ] **Step 4: Run tests**

```
py -m pytest tests/test_bot_stats.py::test_query_stats_today_aggregates tests/test_bot_stats.py::test_query_stats_alltime tests/test_bot_stats.py::test_query_stats_no_trades tests/test_bot_stats.py::test_query_stats_db_unavailable -v
```

Expected: all 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add bot_stats.py tests/test_bot_stats.py
git commit -m "feat: add bot_stats.py with query_stats"
```

---

## Task 2: bot_stats.py — format_telegram + format_terminal

**Files:**
- Modify: `bot_stats.py`
- Modify: `tests/test_bot_stats.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_bot_stats.py — append

def _base_stats(**overrides) -> dict:
    s = {
        "date":               "2026-05-07",
        "today_trades":       5,
        "today_wins":         3,
        "today_losses":       2,
        "today_pnl":          18.50,
        "alltime_trades":     90,
        "alltime_wins":       57,
        "alltime_pnl":        105.93,
        "by_strategy_asset":  {
            ("strategy1", "BTC"): {"wins": 2, "losses": 1, "pnl": 12.50},
            ("strategy2", "ETH"): {"wins": 1, "losses": 1, "pnl": 6.00},
        },
        "last_trade_ts":      "2026-05-07T14:16:00+00:00",
        "consecutive_losses": 0,
        "mode":               "PAPER",
    }
    s.update(overrides)
    return s


def test_format_telegram_contains_key_fields():
    """format_telegram output contains date, WR, P&L, strategy labels, mode."""
    out = bot_stats.format_telegram(_base_stats())
    assert "2026-05-07"          in out
    assert "3/5"                 in out   # WR numerator/denominator
    assert "S1" in out and "EMA" in out
    assert "S2" in out and "Velocity" in out
    assert "PAPER"               in out
    assert "BTC"                 in out
    assert "ETH"                 in out


def test_format_telegram_no_trades_today():
    """When today_trades==0, shows 'No trades today', no strategy sections."""
    out = bot_stats.format_telegram(_base_stats(
        today_trades=0, today_wins=0, today_losses=0, today_pnl=0.0,
        by_strategy_asset={},
    ))
    assert "No trades today" in out
    assert "S1"              not in out
    assert "S2"              not in out


def test_format_telegram_hides_zero_strategy():
    """Strategy section hidden when it has no today trades."""
    out = bot_stats.format_telegram(_base_stats(
        by_strategy_asset={("strategy1", "BTC"): {"wins": 2, "losses": 0, "pnl": 10.0}},
        today_trades=2, today_wins=2, today_losses=0,
    ))
    assert "S1" in out
    assert "S2" not in out


def test_format_telegram_last_trade_never():
    """None last_trade_ts shows 'never'."""
    out = bot_stats.format_telegram(_base_stats(last_trade_ts=None))
    assert "never" in out


def test_format_terminal_no_html_tags():
    """format_terminal output contains no HTML tags."""
    out = bot_stats.format_terminal(_base_stats())
    assert "<b>"   not in out
    assert "</b>"  not in out
    assert "<code>" not in out
    assert "2026-05-07" in out  # content still there
```

- [ ] **Step 2: Run to confirm they fail**

```
py -m pytest tests/test_bot_stats.py::test_format_telegram_contains_key_fields -v
```

Expected: `AttributeError: module 'bot_stats' has no attribute 'format_telegram'`

- [ ] **Step 3: Add format_telegram, format_terminal, and _last_trade_str to bot_stats.py**

Add after `query_stats` function:

```python
def _last_trade_str(last_trade_ts: str | None) -> str:
    if last_trade_ts is None:
        return "never"
    try:
        ts = datetime.datetime.fromisoformat(last_trade_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - ts
        mins  = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{mins} min ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{delta.days}d ago"
    except Exception:
        return "unknown"


def format_telegram(stats: dict) -> str:
    """Format stats dict as an HTML Telegram message."""
    t   = stats["today_trades"]
    w   = stats["today_wins"]
    wr  = f"{w}/{t} ({100 * w / t:.1f}%)" if t > 0 else "0/0"

    def _pstr(v: float) -> str:
        return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

    lines = [
        f"📊 <b>Daily Summary — {stats['date']}</b>",
        _SEP,
        f"Trades: {t}  |  WR: {wr}",
        f"P&L today: {_pstr(stats['today_pnl'])}  |  All-time: {_pstr(stats['alltime_pnl'])}",
        _SEP,
    ]

    if t == 0:
        lines.append("No trades today")
    else:
        by_strat: dict[str, list] = {}
        for (strat, asset), data in stats["by_strategy_asset"].items():
            if data["wins"] + data["losses"] == 0:
                continue
            by_strat.setdefault(strat, []).append((asset, data))

        for strat in sorted(by_strat.keys()):
            label = _STRATEGY_LABELS.get(strat, strat)
            lines.append(f"<b>{label}</b>")
            for asset, data in sorted(by_strat[strat]):
                pnl_str = _pstr(data["pnl"])
                lines.append(
                    f"  {asset:<5} {data['wins']}W/{data['losses']}L   {pnl_str}"
                )

    lines.extend([
        _SEP,
        f"Last trade: {_last_trade_str(stats['last_trade_ts'])}",
        f"Consecutive losses: {stats['consecutive_losses']}",
        f"Mode: {stats['mode']}",
    ])
    return "\n".join(lines)


def format_terminal(stats: dict) -> str:
    """Plain-text version of format_telegram (strips HTML tags)."""
    return re.sub(r"<[^>]+>", "", format_telegram(stats))
```

- [ ] **Step 4: Run tests**

```
py -m pytest tests/test_bot_stats.py -v
```

Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add bot_stats.py tests/test_bot_stats.py
git commit -m "feat: add format_telegram and format_terminal to bot_stats"
```

---

## Task 3: scripts/stats_report.py

**Files:**
- Create: `scripts/stats_report.py`

- [ ] **Step 1: Create the script**

```python
#!/usr/bin/env python3
"""
scripts/stats_report.py — Print trade stats to stdout.

Usage:
    py scripts/stats_report.py
    py scripts/stats_report.py --date 2026-05-06

Reads BOT_DB_FILE env var (default: kalshi_bot.db).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="UTC date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    db_path = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")
    stats   = bot_stats.query_stats(db_path, today_date=args.date)
    stats["consecutive_losses"] = 0
    stats["mode"] = "PAPER"
    print(bot_stats.format_terminal(stats))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it runs**

```
py scripts/stats_report.py
```

Expected: prints stats summary to stdout with current DB data. No traceback.

- [ ] **Step 3: Commit**

```bash
git add scripts/stats_report.py
git commit -m "feat: add scripts/stats_report.py terminal stats CLI"
```

---

## Task 4: bot_loops.py — midnight trigger

**Files:**
- Modify: `bot_loops.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bot_stats.py — append
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio


def test_midnight_trigger_fires_once_per_day():
    """
    _check_daily_stats fires send_telegram exactly once when date changes,
    and does not fire again on subsequent calls with the same date.
    """
    import bot_loops

    sent = []
    async def fake_send(text):
        sent.append(text)

    fake_stats = {
        "date": "2026-05-07", "today_trades": 0, "today_wins": 0,
        "today_losses": 0, "today_pnl": 0.0, "alltime_trades": 0,
        "alltime_wins": 0, "alltime_pnl": 0.0, "by_strategy_asset": {},
        "last_trade_ts": None, "consecutive_losses": 0, "mode": "PAPER",
    }

    with patch("bot_loops.bot_stats.query_stats", return_value=fake_stats), \
         patch("bot_loops.send_telegram", side_effect=fake_send), \
         patch("bot_loops.bot_state._DB_FILE", ":memory:"), \
         patch("bot_loops.bot_state._consecutive_losses", 0), \
         patch("bot_loops.read_config", return_value={"mode": "paper"}):

        bot_loops._last_stats_date = ""  # reset

        async def run():
            await bot_loops._check_daily_stats("2026-05-07")
            await bot_loops._check_daily_stats("2026-05-07")  # same day — no second send
            await bot_loops._check_daily_stats("2026-05-08")  # new day — fires again

        asyncio.run(run())

    assert len(sent) == 2   # once for 05-07, once for 05-08
```

- [ ] **Step 2: Run to confirm it fails**

```
py -m pytest tests/test_bot_stats.py::test_midnight_trigger_fires_once_per_day -v
```

Expected: `AttributeError: module 'bot_loops' has no attribute '_check_daily_stats'`

- [ ] **Step 3: Add _last_stats_date and _check_daily_stats to bot_loops.py**

At the top of `bot_loops.py`, after the existing imports, add:

```python
import bot_stats
```

After all imports and before the first function definition, add the module-level variable:

```python
_last_stats_date: str = ""
```

Add this new async function (add it before `handle_ready_phase`):

```python
async def _check_daily_stats(today: str) -> None:
    """Fire daily Telegram stats summary when UTC date changes."""
    global _last_stats_date
    if today == _last_stats_date:
        return
    _last_stats_date = today
    try:
        _stats = bot_stats.query_stats(bot_state._DB_FILE, today_date=today)
        _stats["consecutive_losses"] = bot_state._consecutive_losses
        _stats["mode"] = read_config().get("mode", "paper").upper()
        await send_telegram(bot_stats.format_telegram(_stats))
    except Exception as _e:
        log.warning("Daily stats send failed (non-fatal): %s", _e)
```

In `main_loop`, inside the `while True:` block, immediately after `midnight_reset()` on line 943 (before the config read), add:

```python
                await _check_daily_stats(
                    datetime.now(timezone.utc).strftime("%Y-%m-%d")
                )
```

The existing import `from datetime import datetime, timezone, timedelta` at line 9 already provides `datetime` and `timezone`.

- [ ] **Step 4: Run the test**

```
py -m pytest tests/test_bot_stats.py::test_midnight_trigger_fires_once_per_day -v
```

Expected: PASSED.

- [ ] **Step 5: Syntax check bot_loops.py**

```
py -c "import ast; ast.parse(open('bot_loops.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add bot_loops.py tests/test_bot_stats.py
git commit -m "feat: add midnight daily stats Telegram trigger to main_loop"
```

---

## Task 5: Remove per-trade Telegram calls — bot_market.py

**Files:**
- Modify: `bot_market.py`

Remove three `send_telegram` call blocks. For each: delete the block and any variable setup lines that exist solely to build the message (i.e., `_placed_ctx`, `_failed_ctx`, `_nofill_ctx` and their associated `_notify_ctx(...)` calls). Keep `log.info` lines.

- [ ] **Step 1: Remove ORDER PLACED block (lines 970–978)**

Find and delete this block in `bot_market.py`:

```python
        _placed_ctx = _notify_ctx(
            asset, ticker, (_placed_elapsed + secs_left) / 60.0,
            _phase_for_eth(asset, _placed_elapsed),
        )
        asyncio.create_task(send_telegram(
            f"<b>[S2 D3 Hybrid] {_placed_ctx} MARKET ORDER PLACED</b>\n"
            f"<b>{side.upper()} -- {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts\n"
            f"Expires in {_placed_mins}m {_placed_secs}s"
        ))
```

Also delete the preceding setup lines that only fed this block:

```python
        _placed_elapsed = seconds_elapsed(market) if market else 0.0
```

Keep the `log.info(f"[live] market...")` line that follows.

- [ ] **Step 2: Remove ORDER FAILED block (lines 1044–1052)**

Find and delete this block:

```python
                _failed_elapsed = seconds_elapsed(market) if market else 0.0
                _failed_ctx = _notify_ctx(
                    asset, ticker, (_failed_elapsed + secs_left) / 60.0,
                    _phase_for_eth(asset, _failed_elapsed),
                )
                await send_telegram(
                    f"<b>[S2 D3 Hybrid] {_failed_ctx} MARKET ORDER FAILED</b>  --  {err_code}\n"
                    f"{side.upper()}  {contracts}x"
                )
```

Keep the `break` that follows.

- [ ] **Step 3: Remove ORDER NOT FILLED block (lines 1170–1178)**

Find and delete this block:

```python
    _nofill_elapsed = seconds_elapsed(market) if market else 0.0
    _nofill_ctx = _notify_ctx(
        asset, ticker, (_nofill_elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, _nofill_elapsed),
    )
    await send_telegram(
        f"<b>[S2 D3 Hybrid] {_nofill_ctx} MARKET ORDER NOT FILLED</b>  --  no liquidity\n"
        f"{side.upper()} -- {'UP' if side == 'yes' else 'DOWN'}  {contracts}x"
    )
```

Keep the `return {...}` that follows.

- [ ] **Step 4: Syntax check**

```
py -c "import ast; ast.parse(open('bot_market.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add bot_market.py
git commit -m "refactor: remove per-trade Telegram pings from bot_market.py"
```

---

## Task 6: Remove per-trade Telegram calls — bot_loops.py and bot_risk.py

**Files:**
- Modify: `bot_loops.py`
- Modify: `bot_risk.py`

- [ ] **Step 1: Remove S2 ORDER FILLED notification from bot_loops.py (lines 529–536)**

Find and delete this block (the `_fill_ctx` setup lines and the `send_telegram` call):

```python
    _fill_ctx = _notify_ctx(
        asset, ticker, (elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, elapsed),
    )
    await send_telegram(
        f"<b>🔵 [S2 D3 Hybrid] {_fill_ctx} {mode_icon} {_strat_tag}</b>  —  {_time_str}\n"
        f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s -> {_expiry_str}"
    )
```

Also delete the setup variables that only fed the deleted block:

```python
    _strat_tag = "REVERSAL" if _is_reversal else "ORDER FILLED"
```

and:

```python
    _fill_ctx = _notify_ctx(
        asset, ticker, (elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, elapsed),
    )
```

Keep `_time_str`, `_expiry_dt`, `_expiry_str`, `_cost`, `_payout`, `_win_pct`, `_ev_str` only if they are used elsewhere in the same function. If not used after the deletion, remove them too.

- [ ] **Step 2: Remove S2 trade close notification from bot_loops.py (lines 676–685)**

Find and delete this block:

```python
        _close_ctx = _notify_ctx(
            asset, pos.get("ticker", ticker), _close_dur_min,
            _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)),
        )
        await send_telegram(
            f"<b>🔵 [S2 D3 Hybrid] {_close_ctx} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
            f"{pos['side'].upper()}  {pos['contracts']} contracts  |  held {_dur_str}\n"
            f"Entry: {pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
            f"{asset}: ${btc_price:,.0f}  vs  Strike: ${pos['strike']:,.0f}"
        )
```

Keep the `await _settle_s1_trade(...)` and `return` that follow. Keep `_cl_ctx` / consecutive-losses block (lines 659–665) — that is a critical alert, not a per-trade ping.

- [ ] **Step 3: Remove S1 ORDER FILLED notification from bot_risk.py (lines 454–461)**

Find and delete this block:

```python
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} ORDER FILLED</b>\n"
        f"<b>{side.upper()} -- {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s"
    )
```

Keep the preceding `log.info(...)` line.

- [ ] **Step 4: Remove S1 outcome notification from bot_risk.py (lines 511–516)**

Find and delete this block:

```python
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  --  {_time_str}\n"
        f"{s1_pos['side'].upper()}  {s1_pos['contracts']} contracts  |  held {_dur_str}\n"
        f"Entry: {s1_pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
        f"Strike: ${s1_pos['strike']:,.0f}"
    )
```

- [ ] **Step 5: Syntax check both files**

```
py -c "import ast; ast.parse(open('bot_loops.py', encoding='utf-8').read()); print('bot_loops OK')"
py -c "import ast; ast.parse(open('bot_risk.py', encoding='utf-8').read()); print('bot_risk OK')"
```

Expected: both print OK.

- [ ] **Step 6: Run all bot_stats tests to confirm nothing broken**

```
py -m pytest tests/test_bot_stats.py -v
```

Expected: all tests PASSED.

- [ ] **Step 7: Commit**

```bash
git add bot_loops.py bot_risk.py
git commit -m "refactor: remove per-trade Telegram pings from bot_loops and bot_risk"
```

---

## Self-Review

**Spec coverage:**
- ✅ `bot_stats.py` with `query_stats`, `format_telegram`, `format_terminal` — Tasks 1–2
- ✅ `scripts/stats_report.py` CLI — Task 3
- ✅ Midnight trigger `_check_daily_stats` in `main_loop` — Task 4
- ✅ `bot_market.py` 3 removals — Task 5
- ✅ `bot_loops.py` 2 removals — Task 6
- ✅ `bot_risk.py` 2 removals — Task 6
- ✅ Kept: consecutive-losses warning (bot_loops 663), fill verification (bot_infra 462), loss limit (bot_risk 98/107), preflight (bot_risk 785), runner crash alerts
- ✅ DB unavailable → no crash (try/except in query_stats)
- ✅ No trades today → "No trades today" shown
- ✅ `last_trade_ts=None` → "never"
- ✅ Strategy sections hidden when zero today trades

**Placeholder scan:** None found.

**Type consistency:** `query_stats` returns dict with `by_strategy_asset` keyed by `(str, str)` tuples. `format_telegram` iterates `stats["by_strategy_asset"].items()` — consistent across Tasks 1 and 2. `_check_daily_stats` passes `today: str` and mutates `_last_stats_date: str` — consistent.
