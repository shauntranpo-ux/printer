# Section 5: SOL Strategy - Research Audit

## Problem Statement

SOL has two characteristics that make a BTC-port strategy wrong:
(1) higher and more volatile beta to BTC, and (2) network-level tail risk
from Solana validator outages. Both are handled explicitly.

---

## Component 1: High-Beta Concurrent BTC Signal (Primary)

**Basis:** Kurihara & Matsumoto (2026), same source as ETH. SOL is a
large-cap and reacts to BTC at lag 0. Beta is higher than ETH's -
approximately 1.5-2.0x on 1-minute returns, reflecting SOL's higher
retail participation and lower liquidity relative to BTC.

**Implementation:** Same BTC 3-min return signal as ETHStrategy, but with
`BETA_ADJ_MAX = 0.12` (vs ETH's 0.10) reflecting the larger expected move.
Beta loaded from `data/betas.json` (refit from 2026 historical data).

---

## Component 2: Momentum-Biased Prior

**Basis:** Grobys & Sapkota (2021), *International Review of Financial
Analysis*. Large-cap cryptos (top-10 by market cap, which includes SOL)
exhibit momentum effects rather than mean reversion. This is consistent
with the strong institutional following and retail herding documented for
top-10 assets.

**Implementation:** A small always-on continuation nudge (`MOMENTUM_BIAS =
0.02`) stacked on top of the variance-ratio regime detector. If above
strike -> +2pp; if below strike -> -2pp. This is independent of BV3 tables
and derived purely from published microstructure research.

---

## Component 3: Solana Network Health Kill Switch

**Basis:** Solana Foundation post-mortems document five significant validator
network incidents between 2022 and 2024:
- 2022-01-21: ~18h outage (forking/consensus failure)
- 2022-05-01: ~7h outage (resource exhaustion)
- 2022-06-01: ~4.5h outage (duplicate transactions)
- 2022-10-01: ~6h outage (non-determinism bug)
- 2024-02-06: ~5h outage (compute overflow bug)

During each event, SOL spot price dropped 3-12% in the first hour while
Kalshi YES contracts would have expired worthless mid-window.

**Implementation:** `check_solana_health()` queries `getHealth` on the
Solana RPC before any SOL decision. If RPC is unreachable, times out, or
returns non-"ok" status, the skip layer returns `"asset_hook: <reason>"` and
the position is skipped. **Fail-safe by design: silence = unhealthy.**
Results cached 30 seconds to avoid hammering public RPC.

---

## Component 4: Final-2-Minute Exhaustion Fade

**Basis:** The exhaustion fade is a microstructure effect: in the final ~2
minutes of a binary window, sharp directional moves (>2 sigma vs trailing vol)
often partially reverse as market makers absorb residual directional flow.

**Implementation:** `exhaustion_fade_adjustment()` activates only when
`seconds_left < 120` AND `|1-min return| / realized_vol > 2`. Magnitude is
small (+/-3pp), direction is opposite to the extreme move.

---

## Intentional Deviations from Legacy

1. No BV3 table - SOL uses Brownian bridge + adjustments
2. Network health kill switch not present in any legacy strategy
3. `min_ev_base: 9` (higher than ETH's 8) due to elevated tail risk
