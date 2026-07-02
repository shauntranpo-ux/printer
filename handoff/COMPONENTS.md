# Components

UI inventory for `Money Printer.html`. Each entry lists: purpose, CSS class(es), source line range, inputs, and behavior notes.

The HTML file is the layout and pixel source of truth. These notes explain the logic.

---

## Battle Bar

**CSS:** `.mc-battle`, `.mc-battle-track`, `.mc-battle-marker`, `.mc-battle-foot`
**Source lines:** 272-319 (CSS), 1035-1075 (render logic in `renderMarketStrip`)

A horizontal tug-of-war bar showing the current YES vs NO balance on a Kalshi market.

**Inputs from data:**
- `yesAsk` - YES ask price in cents
- `noAsk` - NO ask price in cents
- `phase` - determines footer state
- `timer` - displayed in footer when phase is WATCH/READY

**Position formula:**
```js
const yesPct = yesAsk / (yesAsk + noAsk) * 100;
// 0% = far left (NO dominating), 50% = tied, 100% = far right (YES dominating)
```

**Marker color interpolation** (applied at render; transitions smoothly on CSS update):
- yesPct 0-35: interpolate from red (`rgb(214,57,73)`) -> grey (`rgb(136,136,136)`)
- yesPct 35-65: grey
- yesPct 65-100: interpolate from grey -> green (`rgb(27,158,85)`)

**Marker animation:** CSS `transition: left 400ms cubic-bezier(.32,.72,.32,1), background-color 400ms ease-out` - slides on position update.

**Track:** `background: linear-gradient(to right, red/0.20, red/0.06, grey/0.18, green/0.06, green/0.20)` - faint red left, faint green right, neutral center. Center tick mark via `::before`.

**Footer right-side changes by phase:**

| Phase | Footer right |
|---|---|
| WATCH / READY | `⏱ {timer}` (orange if < 1 minute) |
| LOCKED | `● LOCKED` (animated pulse dot) |
| DONE | ` YES` or ` NO` in green/red |
| OFFLINE | `offline` in dim ink |

**Whole bar dimmed to 50% opacity when phase = OFFLINE.**

**Asset card border glow when LOCKED:** `.mc.in-trade` adds amber outer glow.

---

## Market Card

**CSS:** `.mc`, `.mc-head`, `.mc-body`, `.mc-logo`, `.mc-name`, `.mc-status`, `.mc-trend`
**Source lines:** 232-330 (CSS), 1023-1100 (render in `renderMarketStrip`)

One card per asset in the 5-column Overview market strip.

**Inputs:** full MARKETS entry (`sym`, `px`, `ch24`, `phase`, `wp`, `yesAsk`, `noAsk`, `timer`, etc.)

**Subcomponents:**
- Asset logo (32px circle with inline SVG)
- Phase badge (`.mc-status.<phase>`) - color per CSS phase map
- Win probability (large number + "WIN PROB" label)
- Trend arrow (36px circle: up/down/flat, blue/red/grey) - driven by `ch24 > 0.15` / `< -0.15`
- Embedded Battle Bar

**Hover glow:** `.mc-glow.<sym>` - radial gradient using per-asset color variable, `opacity:0` by default, `1` on hover.

---

## Decision Pipeline Row

**CSS:** `.ds-row`, `.ds-asset`, `.ds-votes`, `.ds-gate`, `.ds-ev`, `.ds-decision`, `.ds-detail`, `.ds-field`, `.ds-caret`
**Source lines:** 532-593 (CSS), 1197-1259 (render in `renderSignals`)

One collapsible row per asset in the Decision Pipeline card on Overview.

**Layout:** 7-column grid: `[120px asset] [votes] [spacer] [Gate A chip] [EV chip] [EV value] [decision + caret]`

**Inputs:** `MARKETS[i].sig` object plus `MARKETS[i].side`

**Votes pip row:** 5 dots. Filled blue = votes cast (`sig.vote_count`). Outlined = remaining required (`sig.min_votes`). Empty = beyond threshold. Fraction label: `vote_count / min_votes`.

**Gate A chip:** `.ds-gate.pass` (green) or `.ds-gate.fail` (red). Pass when `!sig.gate_a_block`.

**Gate B (implied by vote pips):** No separate chip - conveyed by pip fill count vs min_votes.

**EV chip:** `.ds-gate.pass` when `sig.ev_pass`, `.ds-gate.fail` otherwise.

**EV value:** `+X.X%` in blue when positive, red when negative. Shows YES EV or NO EV depending on `m.side`.

**Decision pill:** `.ds-decision.trade` (green) or `.ds-decision.skip` (grey).

**Expand on click:** `.ds-row.expanded` shows `.ds-detail` - 2-column grid of 8 `ds-field` rows:
- Raw P(YES), Model P(YES), Market P(YES), Supertrend, YES EV, NO EV, Velocity, Strategy
- If `skip_reason` is set: a full-width row showing the human-readable reason in red.

**Caret:** rotates 90° when expanded (CSS transition).

---

## Equity Chart

**CSS:** `.chart-wrap`, `.chart-line`, `.chart-fill`, `.chart-grid`, `.chart-axis`
**Source lines:** 354-362 (CSS), 1102-1155 (render in `renderEquity`)

SVG line + area chart of cumulative P&L over time.

**Inputs:** array of float values (cumulative P&L). Range toggle: 1D / 1W / 1M / ALL.

**Behavior:**
- 5 horizontal grid lines with Y-axis labels
- X-axis labels at 5 equal intervals
- Area fill below line (gradient from line color at 18% opacity to 0%)
- Line color: `--up` (blue) when last value >= 0, `--down` (red) when negative
- Animated pulse circle on last point (SVG `<animate>`)
- Re-renders on window resize and range button click

**Interaction:** Segment buttons (`1D/1W/1M/ALL`) in card header.

---

## Allocation Pie

**CSS:** `.alloc-wrap`, `.alloc-list`, `.alloc-row`, `.alloc-bar`, `.alloc-sw`, `.alloc-pct`
**Source lines:** 363-372 (CSS), 1157-1195 (render in `renderAllocation`)

Filled pie (no center hole) + list with horizontal bars.

**Inputs:** each asset's `pnlToday` (absolute value for sizing, sign for coloring).

**Per-asset color:** `--btc` (amber), `--eth` (purple-blue), `--sol` (deep purple), `--xrp` (grey), `--doge` (gold-yellow). Exact OKLCH values in `STYLE.md`.

**Pie wedges:** SVG arc paths, separated by 1.25px white strokes. Segments ordered by MARKETS array order.

---

## Order Book Depth + Ladder

**CSS:** `.depth-svg`, `.ladder`, `.ladder-row`, `.ladder-spread`
**Source lines:** 479-477 (CSS), 1301-1337 (render in `renderMarketPanel`)

Two-part component on per-asset tabs.

**Depth chart (top):**
- SVG area chart. Bids (blue fill) on left, asks (red fill) on right, split at the spread.
- Cumulative sum of sizes from spread outward.
- X-axis labels at each price level.

**Ladder (below):**
- Ask rows (`.ladder-row.ask`): price in red, size on right, red background bar scaled to max ask size.
- Bid rows (`.ladder-row.bid`): price in blue, size on right, blue background bar scaled to max bid size.
- Spread separator between asks and bids.
- Data: `ladder.asks` and `ladder.bids` arrays of `[price_cents, size]`.

---

## Confidence Gauge

**CSS:** `.gauge-wrap`, `.gauge-svg`, `.gauge-val`, `.gauge-label`, `.q-bar`
**Source lines:** 483-487 (CSS), 1339-1353 (render in `renderMarketPanel`)

Semicircle arc gauge showing 0-100 confidence score.

**Inputs:** `session.score` (0-100)

**Rendering:**
- Background arc: full semicircle in `--line` color (grey)
- Fill arc: from left to `score/100 * π` radians
- Endpoint dot: circle at arc terminus
- Color: score >= 70 -> blue (`oklch 0.55 0.16 245`), 50-70 -> amber (`oklch 0.82 0.16 75`), < 50 -> red

**Q-bar:** 3-cell quality indicator below the gauge value. Cells 1/2/3 light up at score >= 33/66/85.

---

## Win Heatmap

**CSS:** `.heat-strip`
**Source lines:** 528-529 (CSS), 1373-1381 (render in `renderMarketPanel`)

60-cell horizontal strip showing the last 60 15m windows, colored by outcome.

**Color buckets (from `genSpark` normalized values):**
- > 0.62: blue (strong win)
- > 0.50: light green (win)
- > 0.40: neutral grey (near)
- > 0.30: light red (loss)
- <= 0.30: red (strong loss)

**Wire to:** last 60 `outcome` values from `/api/trades?asset=<sym>&limit=60`.

---

## Trades Table

**CSS:** `table`, `thead th`, `tbody td`, `.td-sym`, `.td-dir`, `.td-fill`, `.td-pnl`
**Source lines:** 384-409 (CSS), 1559-1580 (render in `renderTrades`)

Columns: Time (date + HH:MM stacked), Market (colored swatch + ticker), Dir (UP=blue/DOWN=red), Qty, Entry¢, Exit¢, EV%, Fill (dot indicator), Reason, P&L.

**Fill indicator dot:** green = filled, amber = pending, red = failed.

**Asset filter:** segmented control (All / BTC / ETH / SOL / XRP / DOGE). Filters in-page; no refetch needed if all trades are loaded upfront.

**Export CSV:** `GET /api/export/trades` - triggers download.

---

## Topbar

**CSS:** `#topbar`, `.brand`, `.tabs`, `.tab`, `.topbar-right`, `.status-pill`, `.mode-seg`, `.clock`, `.icon-btn`, `.bot-toggle`
**Source lines:** 52-148 (CSS), 608-647 (HTML), 1624-1664 (JS)

Sticky, blurred. Contains:

| Element | Behavior |
|---|---|
| Brand | "printr" + "15m * KALSHI" sub-label |
| Tab strip | Overview + 5 asset tabs (with coin logos) + Trades |
| Bot toggle | Checkbox -> toggles status pill text "Running"/"Stopped" and `.off` class |
| Status pill | Green pulse dot + "Running". Red when bot off. |
| Mode segment | PAPER / DEMO / LIVE buttons. Active state color-coded. Updates `<body data-mode>` and all `.mode-badge` text. |
| Clock | Live HH:MM:SS UTC clock. |
| Refresh button | Currently no-op; should call refresh on click. |

**Tab coin logos:** `.tab .dot[data-sym]` filled with `cryptoLogo(sym)` on init.

---

## Hero (per-asset)

**CSS:** `.market-hero`, `.hero-row`, `.hero-logo`, `.hero-info`, `.hero-name`, `.hero-px`, `.hero-sub`, `.hero-spark`, `.hero-stats`
**Source lines:** 411-444 (CSS), 1388-1420 (render in `renderMarketPanel`)

Full-width header panel at top of per-asset tab.

- Asset color accent line at top (2px gradient using `--btc`/`--eth` etc.)
- 54px logo circle
- Name + full name + price + 24h change + strike + side
- 60-bar mini sparkline (full-width flex fill)
- Today's P&L + Win Rate on right

**Session tabs** (BTC/ETH only):
- Two `.session-tab` pills: "15-Minute * ACTIVE" + "Hourly * Idle"
- Active pip pulses green
- No JS switching in mock - wire to toggle which session's data is shown in the MKPI grid below

---

## KPI Strip (Overview)

**CSS:** `.kpi-strip`, `.kpi-cell`, `.kpi-cell-l`, `.kpi-cell-v`, `.kpi-div`, `.kpi-trend`
**Source lines:** 185-202 (CSS), 655-673 (HTML)

Single full-width panel with 3 KPI cells separated by 1px dividers: Net P&L (all-time), Win Rate (30d), Today's P&L.

---

## KPI Row (Trades tab)

**CSS:** `.kpi-row`, `.kpi`, `.kpi-label`, `.kpi-value`, `.kpi-sub`
**Source lines:** 204-226 (CSS), 761-787 (HTML)

5-column grid of cards: Total Trades, Win Rate, Avg P&L/trade, Best Streak, Worst DD.

---

## Crypto Logos

**JS function:** `cryptoLogo(sym)` - Source lines: 825-834

Returns an inline SVG string for BTC, ETH, SOL, XRP, DOGE. Used in:
- Tab dots (`.tab .dot[data-sym]`)
- Market card logos (`.mc-logo`)
- Decision pipeline rows (`.ds-asset .logo`)
- Allocation pie list (`.alloc-sw`)
- Per-asset hero (`.hero-logo`)
- Any other logo placement

To add a new asset: add a `case 'XYZ':` to the `cryptoLogo` switch.
