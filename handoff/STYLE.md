# Style Tokens

All tokens are defined in `:root` at lines 11-39 of `Money Printer.html`. Use OKLCH - do not convert to hex/rgb (OKLCH gives perceptually uniform interpolation for the color-shift effects).

---

## Color tokens

### Surface / background

| Token | Value | Use |
|---|---|---|
| `--bg` | `oklch(0.97 0.005 250)` | Page background |
| `--bg-2` | `oklch(0.94 0.006 250)` | Inset backgrounds, hover rows, empty cells |
| `--panel` | `oklch(1 0 0)` | Card / panel backgrounds (pure white) |
| `--panel-2` | `oklch(0.96 0.005 250)` | Active segments, secondary panels |

### Lines / borders

| Token | Value | Use |
|---|---|---|
| `--line` | `oklch(0.88 0.008 250)` | Default border, card edges, table rows |
| `--line-2` | `oklch(0.78 0.01 250)` | Active/hover borders, stronger dividers |

### Text / ink

| Token | Value | Use |
|---|---|---|
| `--ink` | `oklch(0.22 0.01 250)` | Primary text (headings, values) |
| `--ink-2` | `oklch(0.4 0.012 250)` | Secondary text (labels, sub-values) |
| `--ink-3` | `oklch(0.55 0.01 250)` | Tertiary / muted (captions, timestamps) |
| `--ink-4` | `oklch(0.7 0.008 250)` | Disabled / placeholder |

### Semantic

| Token | Value | Use |
|---|---|---|
| `--up` | `oklch(0.55 0.16 245)` | Positive / YES / blue |
| `--up-soft` | `oklch(0.55 0.16 245 / 0.10)` | YES background tint |
| `--up-line` | `oklch(0.55 0.16 245 / 0.30)` | YES border tint |
| `--down` | `oklch(0.55 0.20 20)` | Negative / NO / red |
| `--down-soft` | `oklch(0.55 0.20 20 / 0.10)` | NO background tint |
| `--down-line` | `oklch(0.55 0.20 20 / 0.30)` | NO border tint |
| `--amber` | `oklch(0.55 0.06 250)` | Warning / LOCKED state |
| `--amber-soft` | `oklch(0.55 0.06 250 / 0.10)` | Warning background tint |
| `--accent` | `oklch(0.45 0.15 255)` | Brand accent (DEMO mode, pipeline bars) |
| `--accent-soft` | `oklch(0.45 0.15 255 / 0.10)` | Accent background tint |

### Asset brand swatches

| Token | Value | Asset |
|---|---|---|
| `--btc` | `oklch(0.78 0.16 70)` | Bitcoin (amber-gold) |
| `--eth` | `oklch(0.74 0.10 270)` | Ethereum (purple-blue) |
| `--sol` | `oklch(0.78 0.16 320)` | Solana (magenta-pink) |
| `--xrp` | `oklch(0.6 0.012 250)` | XRP (near-neutral grey) |
| `--doge` | `oklch(0.82 0.13 90)` | Dogecoin (yellow-gold) |

Used in: hero accent line, allocation pie, tab glow effects.

### Shadow

```css
--shadow: 0 1px 0 oklch(1 0 0 / 0.6) inset, 0 1px 2px oklch(0 0 0 / 0.06);
```

Applied to: `.kpi`, `.kpi-strip`, `.card`, `.mc` (all panels).

---

## Phase color map

Applied as `.mc-status.<phase>` class (source lines 264-268):

| Phase | Background | Text color |
|---|---|---|
| `watch` | `var(--bg-2)` | `var(--ink-3)` |
| `ready` | `oklch(0.55 0.16 245 / 0.12)` | `oklch(0.5 0.16 245)` (mid-blue) |
| `locked` | `oklch(0.78 0.13 85 / 0.18)` | `oklch(0.5 0.13 85)` (amber) |
| `done` | `oklch(0.65 0.13 155 / 0.14)` | `oklch(0.42 0.13 155)` (green) |
| `offline` | `var(--panel-2)` | `var(--ink-4)` |

LOCKED also triggers the `.mc.in-trade` border glow:
```css
border-color: oklch(0.78 0.13 85);
box-shadow: 0 0 0 1px oklch(0.78 0.13 85 / 0.4), 0 4px 14px oklch(0.78 0.13 85 / 0.12);
```

---

## Mode color map

Applied via `<body data-mode="...">` + `.mode-badge` CSS (source lines 181-183):

| Mode | Text | Background | Border |
|---|---|---|---|
| `live` | `oklch(0.42 0.13 155)` (green) | `oklch(0.42 0.13 155 / 0.10)` | `oklch(0.42 0.13 155 / 0.4)` |
| `demo` | `var(--accent)` (blue) | `var(--accent-soft)` | `oklch(0.45 0.15 255 / 0.35)` |
| `paper` | `var(--ink-2)` (neutral) | `var(--panel-2)` | `var(--line)` |

---

## Typography

```css
font-family: 'Montserrat', ui-sans-serif, system-ui, -apple-system, sans-serif;
```

Google Fonts load: `Montserrat:wght@300;400;500;600;700;800;900` + `Geist+Mono:wght@400;500;600;700`

**Numeric formatting** - add to any number-displaying element:
```css
font-feature-settings: 'tnum' 1, 'zero' 1;
font-variant-numeric: tabular-nums;
```
The `.mono` class applies this. Use it for prices, P&L values, EV%, timestamps.

**Type scale:**
- 9px - micro labels (UPPERCASE + tracking 0.08-0.12em)
- 10px - secondary labels, sub-values
- 11px - table cells, signal rows, log messages
- 12px - chips, pills, decision labels
- 13px - body, pipeline asset names
- 14px - sub-headers
- 15px - brand
- 18px - KPI card values
- 22px - market price display
- 24px - KPI strip values
- 28px - gauge value
- 32px - hero price

---

## Spacing

Observed grid gaps and paddings (all in px):

| Size | Usage |
|---|---|
| 4 | Tight gaps (pip spacing, small icon margins) |
| 6 | Label-value gaps, sub-text |
| 8 | Default gap between related elements |
| 10 | Card internal gaps, ladder row padding |
| 12 | Grid gaps (`.grid-2`, `.grid-3`, market strip) |
| 14 | Card padding (sides/top), meter stacking |
| 16 | Card padding (`.card`) |
| 18 | Main content padding, card head margin |
| 24 | KPI strip padding, section padding |

---

## Border radius

| Value | Where used |
|---|---|
| 2px | Track bar, heat-strip cells |
| 3px | Meter bars, scrollbar thumb |
| 4px | Phase badges, kpi-label badges, skip-reason chips |
| 5px | Gate chips, decision pills, ladder rows |
| 6px | Active tab bg, brand mark |
| 7px | Segmented controls, clock, icon buttons, mode buttons |
| 9px | `.mkpi` cards, `.tabs` container |
| 11px | All main cards (`.card`, `.kpi`, `.mc`, `.kpi-strip`) |
| 50% | Logo circles, marker dot, pulse dot, trend arrow circle |

---

## Animations

| Animation | Value | Where |
|---|---|---|
| Battle bar marker slide | `400ms cubic-bezier(.32,.72,.32,1)` | `.mc-battle-marker` left + background-color |
| Panel fade-up | `350ms ease` | `.panel.active` opacity + translateY(6px) |
| Allocation bar fill | `800ms cubic-bezier(.6,0,.2,1)` | `.alloc-bar > div` width |
| Meter bar fill | `800ms cubic-bezier(.6,0,.2,1)` | `.meter-bar > div` width |
| Status pulse ring | `1.6s ease-out infinite` | `.pulse-dot::after` scale 0.6→2, opacity 1→0 |
| LOCKED live dot | `1.4s ease-in-out infinite` | `.locked-tag::before` opacity 0.4→1→0.4 |
| Equity last-point pulse | SVG `<animate>` r 3→9, opacity 0.4→0, `1.4s infinite` | Equity chart terminal dot |
| Topbar blur | `backdrop-filter: blur(16px)` | `#topbar` on scroll |
