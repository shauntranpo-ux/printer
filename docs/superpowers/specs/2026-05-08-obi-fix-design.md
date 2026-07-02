# OBI Fix Design - Replace Coinbase WebSocket OBI with Kalshi Contract Depth OBI

**Date:** 2026-05-08
**Status:** Approved

## Problem

`OBIMonitor` (obi_monitor.py) connects to the Coinbase Exchange WebSocket and reads spot crypto orderbook depth (BTC-USD, ETH-USD, etc.) to compute Order Book Imbalance for the S2 strategy gate. This is conceptually wrong: S2 trades Kalshi prediction market contracts, so the relevant OBI is the imbalance of YES vs NO contract depth on Kalshi - not spot crypto depth on Coinbase.

## Solution

Delete `OBIMonitor` and the Coinbase WebSocket entirely. Compute Kalshi contract OBI inside `fetch_orderbook()` - which already receives the raw YES/NO depth arrays from `/markets/{ticker}/orderbook` - and store the result in `bot_state._ticker_obi` keyed by ticker. S2's `_s2_obi_gate` reads from that dict.

Zero new API calls. Zero new async tasks. OBI is always as fresh as the price data it was fetched alongside.

## OBI Formula

In a Kalshi prediction market:
- `yes_arr` (YES asks) = people selling YES contracts -> bearish pressure on YES price
- `no_arr` (NO asks) = people selling NO contracts = implied YES bids -> bullish pressure on YES price

```
OBI = (no_depth - yes_depth) / (no_depth + yes_depth)
```

Positive OBI -> more NO sellers -> market leans YES/bullish.
Negative OBI -> more YES sellers -> market leans NO/bearish.

Sign convention matches the existing `_s2_obi_gate` logic (positive = bullish for YES) so gate behavior is unchanged.

Depth = sum of contract quantities across top N=5 price levels (lowest ask price first).

```python
def _kalshi_obi(yes_arr: list, no_arr: list, top_n: int = 5) -> float | None:
    yes_asks = [(p, q) for p, q in yes_arr if q > 0 and p > 0]
    no_asks  = [(p, q) for p, q in no_arr  if q > 0 and p > 0]
    yes_depth = sum(q for _, q in sorted(yes_asks, key=lambda x: x[0])[:top_n])
    no_depth  = sum(q for _, q in sorted(no_asks,  key=lambda x: x[0])[:top_n])
    total = yes_depth + no_depth
    if total < 1e-9:
        return None
    return (no_depth - yes_depth) / total
```

## Data Flow

```
/markets/{ticker}/orderbook  ->  fetch_orderbook()
                                  ├─ yes_arr, no_arr already parsed
                                  ├─ _kalshi_obi(yes_arr, no_arr, top_n=5)
                                  └─ returns {..., "obi": float | None}

bot_loops.py (after each fetch_orderbook that returns non-None ob):
    bot_state._ticker_obi[ticker] = ob["obi"]
    (runs for primary market fetch AND every window-comparison candidate)

strategy_brain_s2(... ticker=ticker ...)
    _s2_obi_gate(ticker, side, cfg["min_obi"])
        bot_state._ticker_obi.get(ticker)  ->  float | None
        None -> fails open (same as before)
```

## Files Changed

| File | Change |
|------|--------|
| `bot_market.py` | Add `_kalshi_obi(yes_arr, no_arr, top_n=5)` private helper; compute and return `"obi"` key from `fetch_orderbook()` |
| `bot_state.py` | Remove `_obi_monitor`; add `_ticker_obi: dict = {}` and update `__all__` |
| `bot_loops.py` | After each `fetch_orderbook()` returning non-None, write `bot_state._ticker_obi[ticker] = ob["obi"]`; remove any `_obi_monitor` references |
| `bot_strategy.py` | `_s2_obi_gate(asset, side, min_obi)` -> `_s2_obi_gate(ticker, side, min_obi)`; read `bot_state._ticker_obi.get(ticker)` |
| `obi_monitor.py` | **Delete** |
| `bot.py` | Remove `from obi_monitor import OBIMonitor`; remove `OBIMonitor` instantiation and `asyncio.create_task` |

## Error Handling

- **AMM markets** - `/orderbook` returns empty arrays -> `_kalshi_obi([], [])` returns `None` -> `_ticker_obi[ticker] = None` -> gate fails open
- **Network error** - `fetch_orderbook()` returns `None` -> bot_loops skips the store -> stale or absent `_ticker_obi` entry -> gate fails open
- **No stale-seconds check needed** - OBI is written and read within the same cycle; no background task that can lag

## Tests

New file: `tests/test_obi_fix.py`

| Test | Verifies |
|------|----------|
| `test_kalshi_obi_positive_when_no_heavy` | no_depth > yes_depth -> positive OBI |
| `test_kalshi_obi_negative_when_yes_heavy` | yes_depth > no_depth -> negative OBI |
| `test_kalshi_obi_empty_arrays` | both empty -> `None` |
| `test_kalshi_obi_top_n_capping` | only top 5 levels counted even when 10+ levels present |
| `test_fetch_orderbook_returns_obi_key` | mocked HTTP returns known arrays; returned dict has `"obi"` key with correct value |
| `test_s2_obi_gate_bullish_ticker` | `_ticker_obi[ticker] = 0.5`; YES side with min_obi=0.20 -> confirmed |
| `test_s2_obi_gate_bearish_ticker` | `_ticker_obi[ticker] = 0.5`; NO side with min_obi=0.20 -> blocked |
| `test_s2_obi_gate_none_fails_open` | no entry in `_ticker_obi` -> gate returns `(True, None)` |
