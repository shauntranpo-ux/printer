# Data Contract

Every API endpoint the dashboard polls, the JSON shape it expects, and whether it already exists in `server.py`.

---

## Polling cadence

| Panel / component | Endpoint | Interval |
|---|---|---|
| Overview market strip | `/api/market-state` | 30s |
| Overview event log | `/api/market-pulse` | 30s |
| Overview equity chart | `/api/equity` | 60s |
| Overview risk status | `/api/risk` | 60s |
| Overview KPI strip | `/api/pnl` | 60s |
| Per-asset hero + grid | `/api/market/<sym>` | 5s (when tab active) |
| Trades table | `/api/trades` | 60s |
| Topbar bot status | `/api/status` | 10s |

---

## Endpoints

### `GET /api/market-state` — EXISTING (`server.py:568`)

Used by: Overview market strip, Decision Pipeline.

```json
{
  "assets": {
    "BTC": {
      "sym": "BTC",
      "px": 78312.40,
      "ch24": 0.84,
      "dist": 0.11,
      "strike": 78250,
      "ev": 7.6,
      "wp": 0.624,
      "phase": "READY",
      "timer": "6m 28s",
      "pnlToday": 24.40,
      "side": "YES/UP",
      "yesAsk": 54,
      "noAsk": 48,
      "vol": 1.42,
      "sig": {
        "raw_p_yes": 0.612,
        "p_ev": 0.598,
        "market_prob": 0.530,
        "yes_ev": 7.6,
        "no_ev": -3.2,
        "supertrend": 1,
        "velocity": "rising",
        "vote_count": 4,
        "min_votes": 5,
        "ev_pass": true,
        "gate_a_block": false,
        "final_decision": "trade",
        "skip_reason": null,
        "decision_mode": "d3_hybrid"
      }
    },
    "ETH": { "...": "same shape" },
    "SOL": { "...": "same shape" },
    "XRP": { "...": "same shape" },
    "DOGE": { "...": "same shape" }
  }
}
```

**Field glossary:**

| Field | Type | Description |
|---|---|---|
| `sym` | string | Asset ticker: BTC, ETH, SOL, XRP, DOGE |
| `px` | float | Current spot price |
| `ch24` | float | 24h % change |
| `dist` | float | % distance from current price to strike (positive = above strike) |
| `strike` | float | Kalshi 15m contract strike price |
| `ev` | float | Expected value of the selected side (%) |
| `wp` | float | Win probability 0.0–1.0 (used as "WIN PROB" display) |
| `phase` | string | WATCH \| READY \| LOCKED \| DONE \| OFFLINE |
| `timer` | string | Human-readable time remaining in window, e.g. "6m 28s" |
| `pnlToday` | float | Today's P&L for this asset in USD |
| `side` | string | Signal direction: "YES/UP", "NO/DOWN", or "—" |
| `yesAsk` | int | Kalshi YES ask price in cents (0–100) |
| `noAsk` | int | Kalshi NO ask price in cents (0–100) |
| `vol` | float | Realized volatility ratio (vol gate metric) |
| `sig.raw_p_yes` | float | Model P(YES) before calibration (0.0–1.0) |
| `sig.p_ev` | float | Calibrated P(YES) used for EV calculation |
| `sig.market_prob` | float | Market-implied P(YES) from ask prices: yesAsk/(yesAsk+noAsk) |
| `sig.yes_ev` | float | Expected value if betting YES (%) |
| `sig.no_ev` | float | Expected value if betting NO (%) |
| `sig.supertrend` | int | D3 Supertrend direction: +1 (up) or −1 (down) |
| `sig.velocity` | string | Kalshi contract price velocity: "rising" \| "falling" \| "flat" |
| `sig.vote_count` | int | Number of D3 sub-signals agreeing on direction (0–5) |
| `sig.min_votes` | int | Minimum votes required to qualify (4 early window, 5 mid/late) |
| `sig.ev_pass` | bool | True if EV exceeds the configured floor |
| `sig.gate_a_block` | bool | True if Gate A (velocity) is blocking this trade — hard skip |
| `sig.final_decision` | string | "trade" or "skip" |
| `sig.skip_reason` | string\|null | Why the trade was skipped, or null |
| `sig.decision_mode` | string | Strategy name, currently always "d3_hybrid" |

**Skip reason values:**

| Value | Display label |
|---|---|
| `votes_below_threshold` | Votes < threshold |
| `ev_below_threshold` | EV below floor |
| `gate_a_velocity_block` | Gate A · velocity opposed |
| `no_strategy_signal` | No signal |
| `daily_loss_limit` | Daily loss limit |

---

### `GET /api/pnl` — EXISTING (`server.py:461`)

Used by: Overview KPI strip.

```json
{
  "today": {
    "total": 84.60,
    "trades": 12,
    "wins": 7,
    "losses": 5
  },
  "alltime": {
    "total": 1847.20,
    "win_rate_30d": 0.624,
    "trades_30d": 187,
    "wr_delta_30d": 1.8
  }
}
```

---

### `GET /api/trades` — EXISTING (`server.py:253`)

Used by: Trades tab table, per-asset recent trades.

Query params: `?limit=140&asset=BTC` (asset optional)

```json
{
  "trades": [
    {
      "ts": 1746150000000,
      "sym": "SOL",
      "dir": "UP",
      "qty": 1,
      "entry": 49,
      "exit": 100,
      "ev": "6.3",
      "fill": "filled",
      "reason": "vol_expansion",
      "pnl": 0.48
    }
  ]
}
```

| Field | Description |
|---|---|
| `ts` | Unix milliseconds |
| `sym` | Asset ticker |
| `dir` | "UP" or "DOWN" |
| `qty` | Number of contracts |
| `entry` | Entry price in cents |
| `exit` | Exit price in cents (100 = YES won, 0 = NO won, or partial) |
| `ev` | EV at entry (%) as string |
| `fill` | "filled" \| "pending" \| "failed" |
| `reason` | Signal reason (see below) |
| `pnl` | Realized P&L in USD |

**Reason values:** `signal_strong`, `ev_threshold`, `momentum_break`, `reversion`, `vol_expansion`, `price_action`, `strategy_15m`, `strategy_h1`

---

### `GET /api/status` — EXISTING (`server.py:184`)

Used by: Topbar bot status pill.

```json
{
  "running": true,
  "mode": "demo"
}
```

---

### `GET /api/market-pulse` — EXISTING (`server.py:617`)

Used by: Overview recent activity log (10 rows).

```json
{
  "events": [
    { "time": "07:42", "type": "entry", "msg": "SOL → <b>YES @ 58¢</b> · qty 7 · EV 9.4%" },
    { "time": "07:38", "type": "signal", "msg": "SOL: 15m strong UP, vol 1.74" },
    { "time": "07:21", "type": "exit",   "msg": "BTC <b>+$2.30</b> · win @ resolution UP" }
  ]
}
```

Event `type` values: `entry`, `exit`, `signal`, `skip`, `system`

---

### `GET /api/export/trades` — EXISTING (`server.py:408`)

Used by: Trades tab export button. Returns CSV download.

---

### `GET /api/equity?range=1d` — TO BUILD

Used by: Overview equity chart. Returns a list of cumulative P&L values at equal intervals.

Query: `range` = `1d` | `1w` | `1m` | `all`

```json
{
  "range": "1d",
  "points": [0.0, 1.2, -0.4, 3.1, 5.8, "..."],
  "x_labels": ["09:30", "11:00", "12:30", "14:00", "15:30"]
}
```

**Implementation note:** Query the `trades` table, group by time bucket, compute running cumulative P&L. The chart renders points as `(index, value)` — no timestamps needed, just the ordered array.

---

### `GET /api/market/<sym>` — TO BUILD

Used by: Per-asset hero panels (BTC/ETH/SOL/XRP/DOGE tabs).

Example: `GET /api/market/BTC`

```json
{
  "sym": "BTC",
  "sessions": [
    {
      "type": "15m",
      "active": true,
      "expires": "19:15:00 UTC",
      "strike": 78250,
      "dist": 0.11,
      "ev": 7.6,
      "wp": 0.624,
      "yesAsk": 54,
      "noAsk": 48,
      "qty": 5,
      "vol": 1.42,
      "score": 72
    },
    {
      "type": "h1",
      "active": false,
      "expires": "20:00:00 UTC",
      "strike": 78400,
      "dist": 0.18,
      "ev": 5.2,
      "wp": 0.581,
      "yesAsk": 48,
      "noAsk": 54,
      "qty": 0,
      "vol": 1.42,
      "score": 48
    }
  ],
  "ladder": {
    "asks": [[58,12],[57,18],[56,24],[55,31],[54,46]],
    "bids": [[52,38],[51,29],[50,21],[49,15],[48,9]]
  },
  "log": [
    ["00:42", "signal", "15m strategy: <b>UP signal</b>, EV 7.6%, vol 1.42"],
    ["01:14", "entry",  "Entered <b>YES @ 54¢</b> · qty 5 · est P&L +$2.30"]
  ],
  "stats": {
    "wins": 38,
    "losses": 21,
    "wr": 0.644,
    "todayPnl": 24.40,
    "avgEV": 6.8,
    "bestExit": "+$8.40",
    "worstDD": -12.10
  }
}
```

**Notes on sessions:**
- BTC and ETH have two sessions (`15m` + `h1`). SOL, XRP, DOGE have only one (`15m`).
- `sessions[i].score` is the confidence score 0–100 used by the gauge.
- `ladder.asks` and `ladder.bids` are arrays of `[price_cents, size]` pairs. Asks sorted high→low, bids sorted high→low.
- `log` entries: `[time_str, type, html_message]`. Max 8 rows.

---

### `GET /api/risk` — TO BUILD

Used by: Overview Risk Status card.

```json
{
  "daily_loss_limit":   { "current": 0.00,  "max": 50.00 },
  "daily_profit_target":{ "current": 84.60, "max": 200.00 },
  "vol_gate":           { "current": 1.42,  "threshold": 1.80, "asset": "BTC" },
  "ev_floor":           { "current": 7.0,   "pct": 70 },
  "streak":             { "type": "W", "count": 3 }
}
```

---

### `POST /api/bot/toggle` — TO BUILD

Used by: Topbar bot on/off toggle.

Request body: `{ "running": true | false }`

Response: `{ "running": true, "mode": "demo" }`

---

### `POST /api/mode` — TO BUILD

Used by: Topbar PAPER/DEMO/LIVE segment.

Request body: `{ "mode": "paper" | "demo" | "live" }`

Response: `{ "mode": "demo" }`

---

### `POST /api/asset/<sym>/pause` — TO BUILD

Used by: Per-asset Quick Actions "Pause {sym} only".

No body. Response: `{ "sym": "BTC", "paused": true }`

---

### `POST /api/reset_pnl` — EXISTING (`server.py:378`)

Used by: Per-asset Quick Actions "Reset {sym} P&L".

Request body: `{ "asset": "BTC" }` (or omit for all-asset reset)
