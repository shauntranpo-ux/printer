# Section 11 decision framework

## Purpose

These thresholds are LOCKED before the paper-trade session completes.
Any attempt to loosen them after seeing results is p-hacking.

## Per-asset go-live bar

An asset qualifies for live trading at $1/contract stake if ALL are true:

1. **Trade count >= 30** over the 14-day session
   - Rationale: below 30 trades, hit rate is too noisy to draw conclusions
   - If trade count < 30, asset gets another 14-day paper extension,
     NOT go-live

2. **Win rate >= 55%**
   - Rationale: at avg entry 60c, breakeven is ~63%. 55% is below
     breakeven mechanically, BUT the hit rate includes trades at
     entries lower than 60c. What matters is:

3. **Total PnL > $0** on $5/trade paper sizing
   - Rationale: this is the direct profitability test after fees
   - Even with 50% hit rate, PnL can be positive if wins are bigger
     (cheap YES/NO entries that paid out)

4. **Calibration gap <= 15pp** (|avg_model_prob - actual_win_rate|)
   - Rationale: the legacy bot had 30pp gaps. The whole point of this
     refactor is to fix calibration. If the gap is still > 15pp, the
     calibration layer needs more data before going live.

5. **No catastrophic single trade** (loss > 2x stake)
   - Rationale: the EV calculator should never allow this. If it
     happens, there's a bug.

## Per-asset promotion path

| Paper trade outcome | Action |
|---|---|
| All 5 criteria met | Go live at $1/contract for 14 days, then review |
| 3-4 criteria met | Paper extension, investigate the failing criterion |
| 0-2 criteria met | Asset goes back to Section 12/13 for backtest + fix |

## Global kill switches

Regardless of individual asset performance, STOP the paper session
and do not proceed to go-live if:

- Total 14-day paper PnL < -$100 on $5/trade sizing
- Any single 24-hour period shows > $30 loss
- Bot crashes/restarts more than 3x in 14 days
- Any evidence that paper mode accidentally placed a live order
  (check Kalshi account for unexpected orders daily)

## What NOT to do

- Do not change config.json during the 14-day session
- Do not tune Min EV floors based on early results
- Do not flip calibration on/off mid-session
- Do not merge to main during the session
- Do not go live on any asset before the full 14 days complete

## If zero assets pass

If no asset meets the go-live bar after 14 days, this is meaningful
negative evidence. Options:

1. Run Path A (Section 12/13) to fix the backtest and retest
2. Accept that 15-min crypto binaries after 7% Kalshi fee is too
   efficient to extract edge from, and walk away
3. Redesign with different signal sources (on-chain data, DEX flow,
   funding rates) that aren't just technical-analysis derivatives

The honest answer is that option 2 is a reasonable outcome. Not every
strategy works.
