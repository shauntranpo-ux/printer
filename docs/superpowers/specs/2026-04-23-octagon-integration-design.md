# Octagon AI Integration Design

**Date:** 2026-04-23  
**Status:** Approved  
**Scope:** Add Octagon AI as a final confirmation gate before trade execution (paper mode)

---

## Overview

Octagon AI provides natural-language prediction-market research reports. We add it as the last gate in `BaseStrategy.decide()`: after entry price, EV, and momentum checks pass, Octagon confirms whether its model agrees with our intended trade direction. API failures and low-confidence responses fall through - they never block a trade.

---

## Files Changed

| File | Type | Change |
|------|------|--------|
| `src/strategies/signals/octagon_client.py` | New | Singleton HTTP client, two-level cache, URL builder, table parser, `query()` |
| `src/strategies/features.py` | Modified | +4 optional fields to `MarketFeatures` |
| `src/strategies/base.py` | Modified | New Step 6.8 gate after entry-price cap |

No changes to `bot.py`, `feature_builder.py`, or any strategy subclass.

---

## `octagon_client.py`

### HTTP Client

`httpx.Client` with `timeout=3.0`. Module-level singleton (`_http: Optional[httpx.Client]`), created lazily on first call. Provides persistent connection pooling without requiring the sync `decide()` pipeline to become async.

### Cache Structure

Two module-level dicts:

```python
_slug_cache:   dict[str, str]                 # series_ticker → event_slug (permanent)
_report_cache: dict[str, tuple[dict, float]]  # event_ticker  → (parsed_table, fetched_at)
```

- `_slug_cache` key: `series_ticker` (e.g. `"kxbtcd"`) - event slug never changes for a given series, so one Kalshi API lookup per series lifetime.
- `_report_cache` key: `event_ticker` (e.g. `"KXBTCD-26APR2300"`) - all contracts in the same event window share one report.
- TTL: `900s` for 15m markets, `3300s` for hourly.

### URL Construction

Given ticker `KXBTCD-26APR2300-T100000`:

1. `series = ticker.split("-")[0].lower()` → `"kxbtcd"`
2. `event_ticker = "-".join(ticker.split("-")[:-1])` → `"KXBTCD-26APR2300"`
3. `event_slug` - check `_slug_cache[series]`; on miss: `GET /trade-api/v2/events/{event_ticker}`, slugify `event["title"]` (lowercase, replace non-alphanumeric runs with `-`, strip leading/trailing `-`), cache permanently.
4. Final URL: `https://kalshi.com/markets/{series}/{event_slug}/{event_ticker.lower()}`

### `query()` Signature

```python
def query(
    ticker: str,
    strike: float,
    yes_ask: float,
    no_ask: float,
    side: str,
    is_15m: bool,
) -> tuple[Optional[float], Optional[bool], Optional[str], bool]:
    """Returns (model_prob, direction_agrees, confidence, cache_hit)."""
```

Returns `(None, None, None, False)` on any exception - gate is skipped, trade allowed.

### Octagon API Request

```
POST https://api.octagonagents.com/v1/responses
Authorization: Bearer $OCTAGON_API_KEY
Content-Type: application/json

{"model": "octagon-prediction-markets-agent:cache", "input": "<market_url>"}
```

Response text extracted from `response_json["output"][0]["content"][0]["text"]`.

### Table Parsing

The report contains a markdown table under "Executive Verdict" or "Who Wins and Why" with columns: Outcome, Market %, Model %, Why.

Algorithm:
1. Find `|`-delimited lines with ≥ 3 columns.
2. Locate header row containing "Outcome", "Market", "Model".
3. For each subsequent non-separator data row, split on `|` and trim.
4. Match the "Above" row to the current contract: check that `str(int(strike))` appears in the Outcome cell after stripping `$` and `,`.
5. Extract `market_prob = float(market_pct.strip("%")) / 100` and `model_prob` similarly.

### Direction and Confidence Derivation

```python
direction_agrees = (model_prob > market_prob) == (side == "yes")

diff = abs(model_prob - market_prob)
if diff >= 0.05:
    confidence = "high"
elif diff >= 0.02:
    confidence = "medium"
else:
    confidence = "low"
```

When Octagon bullish (model > market), it favors YES. `direction_agrees` is True when that matches the bot's intended side.

---

## `MarketFeatures` Additions

```python
octagon_model_prob:      Optional[float] = None   # Octagon's P(YES)
octagon_direction_agrees: Optional[bool] = None   # True = agrees with bot's side
octagon_confidence:      Optional[str]  = None    # "high"/"medium"/"low" or None (not evaluated)
octagon_cache_hit:       bool           = False   # True if served from TTL cache
```

`None` means not yet evaluated (no Octagon call was made or it errored). `"low"` is set only when Octagon returned a result with < 2pp delta.

---

## Gate in `base.py` - Step 6.8

Inserted after Step 6.75 (entry price cap), before Step 7 (trade decision):

```python
# Step 6.8: Octagon confirmation gate
_oct_prob, _oct_agrees, _oct_conf, _oct_hit = octagon_client.query(
    features.ticker, features.strike,
    features.yes_ask, features.no_ask,
    ev.best_side, self.is_15m,
)
features.octagon_model_prob      = _oct_prob
features.octagon_direction_agrees = _oct_agrees
features.octagon_confidence      = _oct_conf
features.octagon_cache_hit       = _oct_hit
signals.update({
    "octagon_model_prob":       _oct_prob,
    "octagon_direction_agrees": _oct_agrees,
    "octagon_confidence":       _oct_conf,
    "octagon_cache_hit":        _oct_hit,
})

if _oct_prob is not None:
    if self.is_15m:
        # Skip only if direction disagrees AND confidence is not "low"
        if _oct_agrees is False and _oct_conf != "low":
            return Decision(action="skip", side=None, p_model=calibrated_p_yes,
                            reason=f"octagon_veto: conf={_oct_conf} direction_disagrees",
                            contributing_signals={**signals, ...}, expected_value=ev.best_ev)
    else:
        # Hourly: only trade on high/very_high confidence + direction agrees
        if not (_oct_agrees is True and _oct_conf in ("high", "very_high")):
            return Decision(action="skip", side=None, p_model=calibrated_p_yes,
                            reason=f"octagon_veto: conf={_oct_conf} agrees={_oct_agrees}",
                            contributing_signals={**signals, ...}, expected_value=ev.best_ev)
```

`_oct_prob is None` (API error / timeout / no key) → gate skipped entirely → trade proceeds.

---

## Error Handling

All exceptions caught inside `query()`:

| Exception | Behavior |
|-----------|----------|
| `httpx.TimeoutException` | `log.warning("Octagon timeout for {ticker}")` → return `(None, None, None, False)` |
| `httpx.RequestError` | `log.warning(...)` → fallthrough |
| Parse error (`KeyError`, `ValueError`, no matching row) | `log.warning(...)` → fallthrough |
| Missing `OCTAGON_API_KEY` env var | Silent → fallthrough (don't spam logs on every decide()) |
| Any other `Exception` | `log.warning(...)` → fallthrough |

---

## Constraints

- Paper mode only - gate is active in all modes but never blocks on error.
- No changes to hourly strategy subclasses (gate lives in `BaseStrategy`).
- `is_15m=False` strategies apply the stricter hourly gate automatically.
- Event slug lookup re-uses the Kalshi auth headers already available in-process via `os.environ`.
