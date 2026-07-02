# Next Steps - Mocked vs Real

Punchlist for wiring `Money Printer.html` to the live bot. Every item has a status and the exact change needed.

---

## Wiring checklist

### 1. Market strip + Decision Pipeline -> `/api/market-state`  ENDPOINT EXISTS

**Mock location:** `const MARKETS = [...]` (line 839)

**Wire:** Poll `GET /api/market-state` every 30 seconds. Map response `assets` object to the `MARKETS` array shape. Call `renderMarketStrip()` and `renderSignals()` after each response.

```js
async function fetchMarketState() {
  const data = await fetch('/api/market-state').then(r => r.json());
  MARKETS = Object.values(data.assets);
  renderMarketStrip();
  renderSignals();
}
setInterval(fetchMarketState, 30_000);
fetchMarketState();
```

---

### 2. Trades table -> `/api/trades`  ENDPOINT EXISTS

**Mock location:** `const TRADES = (()=>{...})()` (line 906)

**Wire:** On Trades tab activation, fetch `GET /api/trades?limit=140`. Replace `TRADES` array. Re-render on asset filter click.

---

### 3. KPI strip -> `/api/pnl`  ENDPOINT EXISTS

**Mock location:** Hardcoded in HTML (lines 657-673)

**Wire:** Fetch `GET /api/pnl` on load + every 60s. Replace `.kpi-cell-v` text content and `.mode-badge` values.

---

### 4. Bot status + toggle -> `/api/status` + `POST /api/bot/toggle`  PARTIAL

`/api/status` exists (`server.py:184`). The toggle post endpoint needs to be built.

**Wire:** On load, fetch `/api/status` -> set toggle checked state. On toggle change, `POST /api/bot/toggle` with `{ running: bool }`.

---

### 5. Event log -> `/api/market-pulse`  ENDPOINT EXISTS

**Mock location:** Hardcoded in `renderEventLog()` (lines 1264-1275)

**Wire:** Poll `GET /api/market-pulse` every 30s. Replace hardcoded events array with `data.events`.

---

### 6. Equity chart -> `/api/equity`  BUILD THIS

**Mock location:** `const EQ = { '1d': [...], '1w': [...], '1m': [...], 'all': [...] }` (line 898)

**Build:** `GET /api/equity?range=1d|1w|1m|all`

Query the trades table grouped by time bucket, compute running cumulative P&L. Return:
```json
{ "range": "1d", "points": [0.0, 1.2, 3.4, ...], "x_labels": ["09:30", "11:00", "12:30", "14:00", "15:30"] }
```

Wire to equity range buttons: on segment click, fetch the new range and call `renderEquity(data.points, data.x_labels)`.

---

### 7. Per-asset hero pages -> `/api/market/<sym>`  BUILD THIS

**Mock location:** `const MARKET_DATA = { BTC: {...}, ETH: {...}, ... }` (line 928)

**Build:** `GET /api/market/<sym>` - one endpoint for all five assets.

Data needed per asset:
- `sessions[]` - active/inactive, strike, EV, win prob, yes/no ask, qty, vol ratio, confidence score
- `ladder.asks` / `ladder.bids` - top 5 price levels with sizes
- `log[]` - last 8 activity events for this asset
- `stats` - wins, losses, WR, today P&L, avg EV, best exit, worst DD

Wire: call `renderMarketPanel(sym)` after fetching. Poll every 5s when that tab is active.

---

### 8. Risk Status -> `/api/risk`  BUILD THIS

**Mock location:** Hardcoded meters in HTML (lines 728-749)

**Build:** `GET /api/risk`
```json
{
  "daily_loss_limit":    { "current": 0.00,  "max": 50.00 },
  "daily_profit_target": { "current": 84.60, "max": 200.00 },
  "vol_gate":            { "current": 1.42,  "threshold": 1.80, "asset": "BTC" },
  "ev_floor":            { "current": 7.0,   "pct": 70 },
  "streak":              { "type": "W", "count": 3 }
}
```

Wire meter bars to `current/max * 100` percent widths.

---

### 9. Mode segment -> `POST /api/mode`  BUILD THIS

**Mock location:** JS listener (lines 1635-1643) - currently only updates UI, no server call.

**Wire:** After the local UI update, `POST /api/mode` with `{ mode: 'paper'|'demo'|'live' }`. Server should persist this and return it in `/api/status`.

---

### 10. Per-asset pause -> `POST /api/asset/<sym>/pause`  BUILD THIS

**Mock location:** Quick Actions "Pause {sym} only" button (lines 1537-1541) - currently a no-op.

**Wire:** `POST /api/asset/BTC/pause`. After response, update the asset's phase badge to OFFLINE.

---

### 11. Per-asset P&L reset -> `/api/reset_pnl`  ENDPOINT EXISTS

**Mock location:** Quick Actions "Reset {sym} P&L" button (line 1542-1545) - no-op in mock.

**Wire:** `POST /api/reset_pnl` with `{ asset: sym }`. Refetch `/api/pnl` and re-render KPI strip.

---

### 12. Export CSV -> `/api/export/trades`  ENDPOINT EXISTS

**Mock location:** Download button in Trades tab (line 801) - no-op in mock.

**Wire:** On click, navigate to `/api/export/trades` (or open in new tab).

---

### 13. localStorage persistence  RECOMMENDED, NOT IN MOCK

Save and restore on load:
- Active tab (`data-tab` value)
- Active equity range (`1d|1w|1m|all`)
- Bot mode (or read from `/api/status`)

---

### 14. Allocation pie -> real P&L data  WIRE TO `/api/pnl`

**Mock location:** `renderAllocation()` uses `MARKETS[i].pnlToday` (line 1159).

Once `/api/pnl` is wired and has per-asset breakdowns, update `renderAllocation()` to read from that data instead.

---

## Known hardcoded TODOs in the HTML

| Location | What | Fix |
|---|---|---|
| Line 595 | `/* tweaks panel sentinel */` comment - no actual tweaks panel wired | Build a config/tweaks overlay or remove comment |
| Lines 1264-1275 | `renderEventLog()` - fully hardcoded 10 events | Wire to `/api/market-pulse` |
| Lines 898-903 | `EQ` generated by `genEquity(seed, ...)` | Replace with `/api/equity` fetch |
| Lines 906-925 | `TRADES` generated by seeded random | Replace with `/api/trades` fetch |
| Lines 1009-1013 | `genSpark()` used for hero sparkline and heatmap | Wire sparkline to real OHLCV data; heatmap to real trade outcomes |
| Line 1547 | Strategy name hardcoded: `strategy_${sym}_15m` | Pull from `/api/market/<sym>` response |

---

## Session tabs (BTC/ETH vs others)

BTC and ETH have `sessions.length > 1` (15m + hourly). SOL, XRP, DOGE have only one 15m session.

- The `sessionTabsHTML` block (lines 1384-1387) is suppressed for single-session assets (`hasMulti = data.sessions.length > 1`).
- The session tabs are currently decorative in the mock - no JS switches which session's data populates the MKPI grid. Wire them to toggle `activeIdx` and re-render the grid when clicked.

---

## Replacement sequence (suggested order)

1. Wire `/api/market-state` -> market strip + decision pipeline (biggest visible payoff)
2. Wire `/api/pnl` -> KPI strip
3. Wire `/api/status` + bot toggle
4. Wire `/api/market-pulse` -> event log
5. Build + wire `/api/equity` -> equity chart
6. Wire `/api/trades` -> trades table
7. Build + wire `/api/market/<sym>` -> per-asset pages
8. Build + wire `/api/risk` -> risk status meters
9. Build + wire `/api/mode` + `/api/bot/toggle` + `/api/asset/<sym>/pause`
10. Add localStorage persistence for tab + range
