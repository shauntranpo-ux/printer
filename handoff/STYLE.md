# Style Tokens

All tokens live in `:root` near the top of `Money Printer.html`. The theme is a dark
"reactor room" terminal. Use OKLCH - do not convert to hex/rgb (OKLCH gives perceptually
uniform interpolation, and the asset set below was validated in OKLCH space).

The file carries three style blocks, in cascade order:

1. the base `<style>` - layout, components, the token table
2. `<style id="motion">` - ambient blobs, entrance choreography, and the trailing
   `prefers-reduced-motion` kill switch that freezes every animation in the file
3. `<style id="sig">` - the signature layer: typography overrides, hero band, ticker,
   HUD silhouette, light model, instruments. Last in cascade, so it wins on conflicts.

When restyling, add to `#sig` rather than editing earlier blocks.

---

## Color tokens

### Surfaces

| Token | Value | Use |
|---|---|---|
| `--bg` | `oklch(0.145 0.02 265)` | Page background (near-black indigo) |
| `--bg-2` | `oklch(0.185 0.022 265)` | Inset backgrounds, hover rows |
| `--panel` | `oklch(0.205 0.024 265)` | Card / panel backgrounds |
| `--panel-2` | `oklch(0.25 0.026 265)` | Active segments, secondary panels |

### Lines / borders

| Token | Value | Use |
|---|---|---|
| `--line` | `oklch(1 0 0 / 0.09)` | Default border |
| `--line-2` | `oklch(1 0 0 / 0.18)` | Active/hover borders |

### Text / ink

| Token | Value | Use |
|---|---|---|
| `--ink` | `oklch(0.95 0.01 260)` | Primary text |
| `--ink-2` | `oklch(0.80 0.014 260)` | Secondary text |
| `--ink-3` | `oklch(0.62 0.014 260)` | Muted captions, timestamps |
| `--ink-4` | `oklch(0.45 0.012 260)` | Disabled / placeholder |

### Semantic (data polarity - these CARRY MEANING; decorative color never does)

| Token | Value | Use |
|---|---|---|
| `--up` | `oklch(0.75 0.17 160)` | Positive / WIN / YES (mint) |
| `--down` | `oklch(0.66 0.21 15)` | Negative / LOSS / NO (red) |
| `--amber` | `oklch(0.80 0.16 80)` | Warning / LOCKED / S1 identity |
| `--accent` | `oklch(0.66 0.19 285)` | Brand violet (decorative) |
| `--accent-2` | `oklch(0.78 0.14 210)` | Brand cyan (decorative; section numbers, heartbeat) |

Each has a `-soft` background-tint variant; up/down also have `-line` border tints.

### Asset identity hues

| Token | Value | Asset |
|---|---|---|
| `--btc` | `oklch(0.66 0.15 70)` | Bitcoin (amber-gold) |
| `--eth` | `oklch(0.58 0.19 285)` | Ethereum (violet) |
| `--sol` | `oklch(0.66 0.19 335)` | Solana (magenta) |
| `--xrp` | `oklch(0.64 0.13 170)` | XRP (teal) |
| `--doge` | `oklch(0.66 0.12 95)` | Dogecoin (olive-gold) |

Validated as a categorical set: lightness band 0.48-0.67, chroma >= 0.10, adjacent-pair
CVD separation (worst deutan pair dE 26.3), 3:1 contrast on `--panel`. Fixed assignment
per asset - never reorder. The JS mirror is `ASSET_COLOR`; use it, never a local swatch map.

---

## Typography

| Token | Value | Use |
|---|---|---|
| `--font-display` | `'Unbounded', 'Montserrat', sans-serif` | Headers, labels, tabs, card titles (wide face - small sizes are refit in the CORE BREACH block) |
| `--font-mono` | `'JetBrains Mono', ui-monospace, monospace` | EVERY numeral (KPIs, tables, clock, odometer, ticker) |
| `--fs-0..5` | 12 / 14 / 18 / 28 / 48 / 72 px | Type scale; hero odometer uses `clamp(44px, 5.2vw, 72px)` |

Body text stays Montserrat. Numeric elements get `font-feature-settings:'tnum' 1,'zero' 1`
(tabular digits, slashed zero) via the blanket rules at the top of `#sig` - extend that
selector list for new numeric classes rather than styling one-off.

---

## Motion

| Token | Value | Use |
|---|---|---|
| `--spring` | `cubic-bezier(.22,1.2,.3,1)` | Overshoot ease: odometer reels, tab glider, milestone fill |

Rules:
- transform / opacity / filter only (one flagged exception: the cursor-spotlight
  gradient, card-local by design).
- Everything must die under `prefers-reduced-motion` - the kill switch at the end of
  `#motion` freezes any CSS animation/transition automatically; JS effects must check
  `MOTION_OK`.
- Losses stay calm: win/milestone effects escalate; loss states render small and gray.
  Downward milestone crossings fire nothing.

## Signature elements (where to look in `#sig`)

- HERO COMMAND BAND: `#hero-command`, `.odo` (per-digit reel odometer), `#hero-landscape`
  (axis-less all-time equity backdrop), `.hero-chip`, `.hero-mile` (daily milestone bar).
- TICKER: `#ticker` marquee - two `.ticker-set` copies on a `translateX(-50%)` loop; JS
  updates prices in place so the loop never jumps.
- HUD: notched `clip-path` on `.mc` / `.market-hero` / `#hero-command`, `.hud-corners`
  brackets, CSS-counter section numbers on `.card-title::before`, scanlines confined to
  the hero + market-hero, `.mc-ring` conic energy ring on LOCKED cards (replaces the old
  box-shadow tradeGlow).
- LIGHT: cursor spotlight (`body.spot-on .card::after` at `--mx/--my`), pointer parallax
  (`--px/--py` on body; ambient + hero landscape drift).
- HEARTBEAT: `#pulse-line .pulse-streak` sweeps the topbar edge on every successful
  `/api/*` poll (fetch wrapper in the signature JS layer).
- CORE BREACH (last section of `#sig` + FX engine in the script): full-bleed hero stage
  with reactor ring assembly (`#hero-rings`) and perspective horizon (`#hero-horizon`),
  circuit bus traces from the market pods into the core (`#circuit-bus`), frameless
  overview chart bands with angled seams, gutter guide lines (`body::before`), and the
  `_fx` canvas particle engine (settle surge / shockwave / milestone wave / equity
  tracer / ambient embers) that replaced confetti. Canvas colors live in `_FX_ASSET` -
  keep it in sync with the `:root` asset set.
