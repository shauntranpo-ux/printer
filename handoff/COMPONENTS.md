# Components

UI inventory for `Money Printer.html`. Each entry lists purpose, CSS class(es), and
behavior notes. Find source by searching the class name - the file moves too often for
line numbers to stay true. The HTML file is the layout and pixel source of truth; base
styles live in the first `<style>`, motion in `<style id="motion">`, and the signature
layer (hero, ticker, HUD, instruments) in `<style id="sig">`, which is last in cascade.

Polarity everywhere: `--up` mint = positive/WIN/YES, `--down` red = negative/LOSS/NO.
Decorative color (violet `--accent`, cyan `--accent-2`) never carries data meaning.

---

## Hero Command Band (Overview)

**CSS:** `#hero-command`, `.hero-core`, `.hero-eyebrow`, `.odo`, `.hero-meta`,
`.hero-chip`, `.hero-mile`, `.hero-satellites`, `#hero-landscape`
**JS:** `_heroSync` (1s), `renderOdometer`, `renderHeroLandscape`, `_renderStreak`,
`_renderMilestone` in the signature layer at the end of the script.

The centerpiece: combined S1+S2 all-time net P&L as a giant per-digit odometer.

- **Odometer:** each digit is an `.odo-digit` slot clipping an `.odo-reel` of ten
  stacked glyphs; a digit change updates `--d`, the reel rolls via
  `translateY(calc(var(--d) * -1em))` with the `--spring` bezier, staggered 40ms/digit
  from the right. `renderOdometer` rebuilds DOM only when the string SHAPE changes.
- **Value source:** `_heroSync` reads `#kpi-alltime-val` + `#kpi-s1-alltime-val` text via
  `_numOf` (DOM scanner - survives re-renders, touches no fetch code) and sums.
- **Landscape:** `#hero-landscape` is an axis-less all-time equity area
  (`buildStrategyEquity(null,'all')` + `_smoothPath`), bottom-masked, drifting a few px
  with the pointer. Re-renders when the settle count or width changes.
- **Chips:** today P&L chip, win-streak chip (tiers at 2/4/7 consecutive wins escalate
  mint -> glow -> pulse+bolt; losing streaks render small and gray), and the daily
  milestone bar (today vs the Daily Profit Target parsed from `#risk-meters`; $200
  fallback). Upward quarter-milestone crossings fire `_fireConfetti(320,2)` + the
  `#edge-flash` screen-edge glow; downward crossings fire nothing.
- **Satellites:** BOTH original KPI strips (`#kpi-strip-s2`, `#kpi-strip-s1`) moved
  verbatim inside `.hero-satellites` - every `kpi-*` id intact, `fetchPnl`/`fetchPnlS1`
  untouched.
- Skeleton shimmer until the first `/api/trades` resolves (`window._tradesLoaded`).

---

## Ticker Tape

**CSS:** `#ticker`, `.ticker-track`, `.ticker-set`, `.tk-item`
**JS:** `renderTape` (3s), `_tkSetHTML`, `_tkSettles`

30px marquee under the topbar: five asset prices + 24h change, then the last 5 settles
(WIN/LOSS + P&L). Two identical `.ticker-set` copies scroll `translateX(-50%)` on a 45s
linear loop (pause on hover). Prices update IN PLACE via `[data-tk]` spans so the loop
never restarts; the track only rebuilds when a new settle arrives. Hidden <= 900px.

---

## Data Heartbeat

**CSS:** `#pulse-line`, `.pulse-streak`  **JS:** fetch wrapper + `_pulseBeat`

A 2px cyan streak sweeps the topbar's bottom edge whenever a real `/api/*` poll
succeeds (throttled to ~1/s). The same wrapper sets `window._tradesLoaded`, which flips
skeletons to live content. MOTION_OK-gated.

---

## Market Card

**CSS:** `.mc`, `.mc-head`, `.mc-body`, `.mc-logo`, `.mc-name`, `.mc-status`,
`.hud-corners`, `.mc-ring`  **JS:** `renderMarketStrip`

One card per asset in the 5-column Overview strip. Notched HUD silhouette
(`clip-path` 14px cut corners).

- Phase badge (`.mc-status.<phase>`): WATCH / READY / LOCKED / DONE / OFFLINE.
- EV readout + S1/S2 direction arrows + gate-pass badges.
- **Countdown ring:** `.mc-logo::after` conic ring shows time left in the 15-min window
  via `--frac` (set in the template from `secsLeft/900`). Amber-neutral.
- **Corner brackets:** `.hud-corners` L-corners fade in on hover/selected/in-trade.
- **Energy ring:** `.mc-ring` conic sweep masked to a 1.5px border band on LOCKED cards
  (replaces the old box-shadow tradeGlow), plus a scan-line sweep (`.mc.in-trade::after`).
- **Hover:** per-asset radial tint (`.mc-glow.<sym>`), 3D tilt (motion layer), and FOCUS
  DIMMING - the other four cards recede via
  `.market-strip:has(.mc:hover) .mc:not(:hover)` (the rule sets `animation:none` to
  release cardIn's opacity fill; keep that if you touch it).

## Battle Bar

**CSS:** `.mc-battle`, `.mc-battle-track`, `.mc-thrust`, `.mc-battle-marker`,
`.mc-battle-foot`

Tug-of-war for YES vs NO. `yesPct = yesAsk / (yesAsk + noAsk) * 100`.

- **Thrust bars:** `.mc-thrust.yes` grows from the left (mint gradient),
  `.mc-thrust.no` from the right (red gradient), meeting at the marker; width
  transitions 400ms.
- Marker color interpolates red -> gray -> mint across yesPct 0-35-65-100 (computed in
  JS at render).
- In-trade: pulsing spark on the marker (`.mc.in-trade .mc-battle-marker::after`).
- Footer right by phase: timer / LOCKED tag / settled / offline. Bar dims 50% when
  OFFLINE.

---

## Equity Chart

**CSS:** `.chart-wrap` (280px), `.chart-grid`  **JS:** `renderEquity`,
`buildStrategyEquity`, `_smoothPath`

SVG smoothed line + area of cumulative P&L, one per strategy (S2 + S1 cards). Draw-in
via stroke-dash offset; gridlines re-fade on every render (`gridPulse`) so refreshes
read as a re-lock. Line color by sign of the LAST value (mint/red). Range seg
1D/1W/1M/ALL. Empty state -> radar (below).

## Allocation Donut

**JS:** `renderAllocForStrategy(sv, donutId, listId)`

Donut (hole shows NET P&L) + per-asset list with bars. Colors come from `ASSET_COLOR` -
never a local map. Segments sweep in with staggered delays. Empty state -> radar.

## Radar Empty State

**CSS:** `.empty-radar`, `.radar-sweep`, `.radar-label`  **JS:** `_emptyState(label)`

Every bare "no data" message renders as a mini radar: concentric rings + rotating conic
sweep + caps label ("SCANNING * AWAITING FIRST SETTLE"). Before the first
`/api/trades` response it renders a `.skel-block` shimmer instead.

---

## Confidence Reactor (per-asset)

**CSS:** `.gauge-wrap`, `.gauge-arc`, `.gauge-needle`, `.gauge-val`, `.q-bar`
**JS:** gaugeHTML inside `renderMarketPanel`

Multi-ring semicircular instrument for the 0-100 session score:

- 41-tick outer ring (majors every 5th, brighter).
- Score arc draws in via `gaugeDraw` (dashoffset `--len` -> `--off`).
- Needle sweeps from -90deg to `--rot` with an overshoot bezier (`needleSweep`);
  `transform-box:view-box` keeps the pivot at the hub.
- Color by score: >= 70 mint, 50-70 amber, < 50 red. Glowing hub.
- Re-renders (and replays) only when `renderMarketPanel` runs - 30s poll or tab switch.

## Win Heatmap

**CSS:** `.heat-strip` - 60-cell strip; hover ripple (`scaleY` + brightness).

---

## Trades Table

Columns: Time, Market (swatch from `ASSET_COLOR` + ticker), Strat, Dir (UP mint / DOWN
red), Qty, Entry¢, Exit¢, EV%, Fill dot, Reason, P&L. Asset filter seg + CSV export
(`GET /api/export/trades`). All numerals JetBrains Mono tabular.

---

## Topbar + Tabs

**CSS:** `#topbar`, `.tabs`, `.tab`, `#tab-glider`, `.status-pill`, `.mode-seg`,
`.clock`

Sticky, blurred. Brand / tab strip / bot toggle / status pill / mode seg / clock /
P&L-reset button.

- **Tab glider:** `#tab-glider` is one absolutely-positioned pill behind the tabs,
  moved by transform/width (spring bezier) on tab clicks; `positionGlider` re-anchors on
  resize/load/fonts.ready. `.tab.active` itself is transparent - the glider IS the
  active state. `switchTab` untouched.
- Section headers get CSS-counter numbers (`.card-title::before`, per panel) - the hero
  eyebrow hardcodes `00`.
- Panel switches wipe in via `panelWipe` clip-path.

---

## Light Model

- **Cursor spotlight:** `body.spot-on .card::after` / `.kpi::after` paint a 300px
  radial highlight at `--mx/--my` (set by one rAF-throttled pointermove); hover-only,
  `pointer:fine` only. The one deliberate non-transform paint, card-local.
- **Pointer parallax:** the same listener sets `--px/--py` on body; `#ambient` translates
  +-22px, `#hero-landscape` +-8px.

## Reduced Motion

The `#motion` kill switch freezes all CSS animation/transition; `#ambient` hides. JS
effects (confetti, heartbeat, spotlight, parallax, celebrations) check `MOTION_OK`.
The ticker freezes readable at rest; the odometer still updates content instantly.

---

## Crypto Logos

**JS:** `cryptoLogo(sym)` returns inline SVG for BTC/ETH/SOL/XRP/DOGE. Used in tab
dots, market-card logos, ticker items, allocation list, per-asset hero. To add an
asset: extend the `cryptoLogo` switch AND add its validated hue to the `:root` asset
set + `ASSET_COLOR`.
