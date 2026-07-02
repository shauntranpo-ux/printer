# Printr · Trading Desk - Handoff Brief

## What this is

**Printr** is a real-time trading dashboard for a Kalshi binary options bot running 15-minute crypto markets (BTC, ETH, SOL, XRP, DOGE). The bot buys YES or NO contracts on whether a coin will close above or below its 15-minute strike price. This dashboard surfaces live market state, the bot's signal pipeline, equity curve, and full trade history - all in one page, no page reloads.

`Money Printer.html` is the design source of truth (pixel-perfect mockup, all data mocked in JS). The rest of the handoff docs tell you what each field means and which API to wire it to.

---

## Tabs

### Overview
Full-width control tower. Top-to-bottom:
1. **KPI Strip** - Net P&L (all-time), Win Rate (30d), Today's P&L. Mode badge (PAPER/DEMO/LIVE) is color-coded.
2. **Market Strip** - 5 asset cards (BTC/ETH/SOL/XRP/DOGE), each with phase badge, win probability, trend arrow, and embedded **Battle Bar** (YES vs NO live tug-of-war).
3. **Equity Curve + Allocation Pie** - side-by-side. Equity is a time-range line chart (1D/1W/1M/ALL). Allocation is a filled pie showing each asset's share of today's P&L.
4. **Decision Pipeline** - one row per asset showing D3 hybrid vote pips, Gate A/B status, EV check, final decision (TRADE/SKIP). Click any row to expand all 8 signal fields.
5. **Recent Activity** - 10-row unified event log across all assets (entries, exits, signals, skips).
6. **Risk Status** - meters for daily loss limit, profit target, vol gate, EV floor, and streak.

### BTC / ETH / SOL / XRP / DOGE (shared panel)
Full per-asset breakdown. Clicking an asset tab renders:
1. **Hero** - logo, current price, 24h % change, strike price, side (YES/NO), 60-bar sparkline, today's P&L + win rate.
2. **Session tabs** - BTC and ETH have two strategies (15-Minute + Hourly). SOL/XRP/DOGE have only 15-Minute.
3. **6-stat grid** - Strike (with expiry time), Distance from strike (%), EV (vs floor), Win Prob, Vol Gate (vs threshold), Position (qty + entry price).
4. **Order Book Depth + Ladder** - cumulative bid/ask area chart. Ladder shows top 5 bids and asks with size bars.
5. **Confidence Gauge** - semicircle 0-100 score. Color: red < 50, amber 50-70, blue ≥ 70. Q-bar tier indicator below.
6. **Win Heatmap** - 60-cell strip of last 60 15m windows, colored by outcome.
7. **Recent Trades** - last 8 trades for this asset (Time, Dir, Qty, Entry, Exit, EV, Fill, Reason, P&L).
8. **Activity Log** - per-asset event feed (entries, exits, signals, skips).
9. **Performance meters** - Win Rate, Avg EV, Best Win, Worst DD.
10. **Strategy Signals** - 6 rows: EV ≥ floor, Vol gate, Win prob, Confidence, Strike distance, Price validation.
11. **Quick Actions** - View full backtest / Pause {sym} only / Reset {sym} P&L.

### Trades
Global trade ledger.
1. **5-stat KPI row** - Total Trades, Win Rate, Avg P&L/trade, Best Streak, Worst DD.
2. **Trades Table** - paginated (80 rows shown), filter by asset. Columns: Time (date + HH:MM), Market, Dir (UP/DOWN), Qty, Entry, Exit, EV, Fill, Reason, P&L. Export CSV button.

---

## Phase model

Every asset is in one of five phases at any given moment:

| Phase | What it means |
|---|---|
| `WATCH` | Bot is monitoring. The entry minute (minute 5 of 15) has not arrived. |
| `READY` | Signal qualified. Waiting for the entry trigger. |
| `LOCKED` | Active open position on this market. |
| `DONE` | Window resolved. Awaiting the next 15-minute window. |
| `OFFLINE` | Asset disabled, or no live data feed. |

The phase drives the **Battle Bar** footer (timer vs LOCKED tag vs ✓/✗ vs offline), the asset card border glow (LOCKED = amber), and the phase badge color.

---

## Mode model

| Mode | What it means |
|---|---|
| `PAPER` | Simulated trades, no Kalshi fills. |
| `DEMO` | Historical replay using Kalshi data. |
| `LIVE` | Real money, real fills on Kalshi. |

The mode badge (`DEMO` / `LIVE` / `PAPER`) appears in the KPI strip and topbar. Color-coded: LIVE = green, DEMO = blue, PAPER = neutral.

---

## Persistence

| State | Where it lives |
|---|---|
| Bot on/off | Server-side (persist via `POST /api/bot/toggle`) |
| Mode (PAPER/DEMO/LIVE) | Server-side (`POST /api/mode`) |
| Active tab | Recommend `localStorage` - not in mock yet |
| Equity range (1D/1W…) | Recommend `localStorage` - not in mock yet |
| Tweaks panel (if built) | `localStorage` in mock; move server-side |
