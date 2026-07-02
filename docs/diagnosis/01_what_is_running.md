# Step 1 Diagnosis: What Is Actually Running in Paper Mode

**Date:** 2026-05-07  
**Repo:** kalshi-bot (Printer bot)  
**Status:** STOP - awaiting user confirmation before Step 2

---

## TL;DR - Critical Finding

> **The "dual brain" (STRATEGY_A vs STRATEGY_B) described in the task prompt is NOT connected to the live paper-trade loop. Those classes live in the backtesting infrastructure only (`backtest_engine.py`, `cli.py`). They have never been wired into `bot_loops.py`.
> What is actually paper-trading is `strategy_brain_s2` alone. `strategy_brain_s1` (PRINTER_BRAIN) runs in parallel but has produced ZERO trades in the DB.**

---

## 1.1 Execution Trace

```
runner.py
  └── _start_bot() -> subprocess: python bot.py  (one instance, config "btc15m")
        └── bot.py:main()  [bot.py:48]
              └── await main_loop()  [bot.py:139, imported from bot_loops.py:33]
                    └── bot_loops.py:main_loop()  [bot_loops.py:860]
                          └── per-asset loop -> evaluate_market_and_maybe_trade()  [bot_loops.py:~185]
                                ├── strategy_brain_s2(...)  called at bot_loops.py:209
                                │     labeled "Printer Brain - primary decision engine"
                                │     -> dispatches to FifteenMinStrategy (src/strategies/)
                                │     -> writes strategy_variant = "strategy2"
                                │
                                └── strategy_brain_s1(...)  called at bot_loops.py:217
                                      labeled "printer_brain v3" in docstring
                                      -> BV3 empirical table + momentum + velocity + market anchor
                                      -> writes strategy_variant = "strategy1"
```

**Exact call sites:**

| Strategy | File | Line | Function |
|---|---|---|---|
| `strategy_brain_s2` (S2 / "Printer Brain" comment) | `bot_loops.py` | 209 | `evaluate_market_and_maybe_trade()` |
| `strategy_brain_s1` (S1 / "printer_brain v3" docstring) | `bot_loops.py` | 217 | `evaluate_market_and_maybe_trade()` |

Neither `STRATEGY_A` (`strategies/strategy_a/model.py`) nor `STRATEGY_B` (`strategies/strategy_b/contract_dislocation.py`) are imported or called anywhere in the live bot files (`bot.py`, `bot_loops.py`, `bot_strategy.py`, `bot_risk.py`, `bot_infra.py`).

---

## 1.2 Dispatch Logic - How the Two Brains Are Combined

Both brains are called **in series** for every evaluated market window:

```python
# bot_loops.py:209-223
brain    = strategy_brain_s2(...)          # S2 runs first, always
brain_s1 = strategy_brain_s1(...)         # S1 runs second, always
await _execute_s1_trade(session, brain_s1, ...)  # S1 trade placed independently (bot_risk.py:351)

# Then S2 decision flow continues (lines 224-500+):
do_trade = brain["action"] == "trade"      # S2 result drives the main execution path
```

**S1 (PRINTER_BRAIN) path:** `_execute_s1_trade()` in `bot_risk.py:351`.
- If `brain_s1["action"] == "trade"`, places order and writes `strategy_variant = "strategy1"`.
- Completely independent of the S2 path - a window can generate S1 trade, S2 trade, both, or neither.

**S2 (FifteenMinStrategy) path:** Main execution block in `bot_loops.py:224-500`.
- Uses `brain["action"]`, `brain["side"]`, `brain["win_prob"]` etc.
- Writes `strategy_variant = "strategy2"`.

**Combination type:** Parallel / independent. Not a vote. Not a filter. Each can fire on the same window.

---

## 1.3 Which Strategies Are Actively Producing Trades in Paper Mode

| Strategy | Brain Function | Trades in DB | Active? |
|---|---|---|---|
| S1 (PRINTER_BRAIN) | `strategy_brain_s1()` | **0** | NO - always skipping |
| S2 (FifteenMinStrategy) | `strategy_brain_s2()` | **90** | YES |
| STRATEGY_A (logistic regression) | `StrategyAModel` | N/A - not wired | BACKTESTING ONLY |
| STRATEGY_B (contract dislocation) | `ContractDislocationDetector` | N/A - not wired | BACKTESTING ONLY |

**S1 is silent.** DB query confirms: all 90 trades have `strategy_variant = 'strategy2'`.  
S1's EV gate (`min_ev_base_s1` per asset: BTC=8%, ETH=12%, SOL=5%, XRP=8%) combined with the vol gate (`vol_ratio >= 1.80`) is suppressing every trade. Either the BV3 table rarely generates `ev >= min_ev_base_s1` in current market conditions, or vol gate fires first.

---

## 1.4 `strategy_name` / `strategy_variant` Column in Trades DB

**Column exists:** `strategy_variant` (TEXT) in `kalshi_bot.db.trades`.

**Observed values:**
```
strategy_variant  |  count  |  latest_ts
strategy2         |  90     |  2026-04-16T15:30:00+00:00
strategy1         |  0      |  -
```

**Gap:** `model_prob` column exists (stores the brain's predicted win probability), but there is no column storing the S1 brain's predicted probability when S1 skips. S1 skip decisions are logged to `market_log` table only (via `_log_entry()`), not to `trades`. This means **there is no way to compute S1 calibration gap from the trades DB** - S1 has no trade rows to analyze.

---

## 1.5 Naming Mismatch vs. Task Prompt

The task prompt uses names that partially match but are not exact:

| Task Prompt Name | Actual Code Name | Location | In Live Loop? |
|---|---|---|---|
| PRINTER_BRAIN (`printer_brain()` in bot.py) | `strategy_brain_s1()` in `bot_strategy.py:413` | Docstring: "printer_brain v3" | YES, but 0 trades |
| STRATEGY_A | `StrategyAModel` in `strategies/strategy_a/model.py` | Backtesting only | NO |
| STRATEGY_B | `ContractDislocationDetector` in `strategies/strategy_b/contract_dislocation.py` | Backtesting only | NO |
| STRATEGY_C | STRATEGY_C in `strategies/strategy_c/` | Hourly markets, out of scope | NO (not in this loop) |
| - (not named in prompt) | `strategy_brain_s2()` / FifteenMinStrategy | `bot_strategy.py:122` + `src/strategies/` | YES, 90 trades |

**Key point:** `printer_brain()` as a standalone function in `bot.py` does not exist in the current codebase. It was refactored into `strategy_brain_s1()` in `bot_strategy.py`. The `docs/section2_btc_audit.md` documents the legacy version.

The comment at `bot_loops.py:208` labels `strategy_brain_s2` as "Printer Brain - primary decision engine." This conflicts with `strategy_brain_s1`'s docstring calling itself "printer_brain v3." Both names exist in the code for different functions. **This ambiguity must be resolved before proceeding with further diagnostics.**

---

## What Actually Needs Diagnosing

Based on these findings, the actual diagnostic target for subsequent steps is:

- **S2 / `strategy_brain_s2` / FifteenMinStrategy** - 90 paper trades, ALL assets claim `strategy2`. This is the only active strategy. Steps 2-6 should focus here.
- **S1 / `strategy_brain_s1` / PRINTER_BRAIN** - 0 trades. Root cause of silence must be identified separately (Step 2 can check if S1's EV gate is too tight or vol gate is blocking everything).
- **STRATEGY_A and STRATEGY_B from `strategies/`** - not in scope for live diagnosis. They exist in backtesting scaffolding only.

---

## Files Consulted

| File | Purpose |
|---|---|
| `runner.py` | Entry point, launches `bot.py` per `strategies.json` entry |
| `strategies.json` | One entry: `"btc15m"` with `config.json` / `kalshi_bot.db` |
| `bot.py` | Bootstrap: calls `main_loop()` from `bot_loops.py` |
| `bot_loops.py:860` | `main_loop()` - permanent 10-second loop |
| `bot_loops.py:209,217` | Both brain calls, then `_execute_s1_trade` |
| `bot_strategy.py:122` | `strategy_brain_s2()` - dispatches to FifteenMinStrategy |
| `bot_strategy.py:413` | `strategy_brain_s1()` - BV3 + momentum + velocity (PRINTER_BRAIN) |
| `bot_risk.py:351` | `_execute_s1_trade()` - independent S1 trade execution |
| `strategies/strategy_a/model.py` | `StrategyAModel` - backtesting only |
| `strategies/strategy_b/contract_dislocation.py` | `ContractDislocationDetector` - backtesting only |
| `kalshi_bot.db` | 90 trades, all `strategy_variant='strategy2'`, zero `strategy1` |
