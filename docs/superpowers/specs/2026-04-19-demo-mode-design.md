# Demo Mode — Backend Design Spec
**Date:** 2026-04-19  
**Scope:** `bot.py` only — no UI, no dashboard, no src/kalshi_botv3 changes  
**Status:** Approved (pending user sign-off)

---

## Context

The bot currently has two trading modes selected via `config.json` → `"mode"` or env var `BOT_MODE`:
- `"paper"` — simulated fills, no API calls
- `"live"` — real Kalshi API at hardcoded `KALSHI_BASE_URL`, credentials from `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY` env vars

We are adding a third mode:
- `"demo"` — real Kalshi demo API (`https://demo-api.kalshi.co/trade-api/v2`), fake money, credentials from `KALSHI_DEMO_API_KEY` + `KALSHI_DEMO_PRIVATE_KEY` env vars

**Demo mode is NOT simulated.** It follows the exact same code path as live — the only differences are base URL and credentials.

---

## Confirmed URLs

| Mode | Base URL | Source |
|------|----------|--------|
| live | `https://api.elections.kalshi.com/trade-api/v2` | existing bot.py constant (unchanged) |
| demo | `https://demo-api.kalshi.co/trade-api/v2` | Kalshi official docs (confirmed) |

The `src/kalshi_botv3` package uses `https://trading-api.kalshi.com/trade-api/v2` for its prod client — this discrepancy is noted but out of scope; bot.py's live URL is working and untouched.

---

## Approach: Global-at-Startup (Option A)

At startup, detect the active mode, then:
1. Set the `KALSHI_BASE_URL` global to the correct URL for that mode
2. Load the correct credentials into the existing `api_key` / `private_key` globals

All 18+ existing references to `KALSHI_BASE_URL`, `api_key`, and `private_key` throughout bot.py continue to work with zero changes. No call-site updates. No new function signatures.

Safety is enforced by startup assertions before any orders are placed.

---

## Mode Selection (unchanged pattern)

Mode is read from `config.json` → `"mode"` key, overridable via `BOT_MODE` env var:

```json
{ "mode": "demo" }
```

or

```bash
BOT_MODE=demo
```

No new mechanism is introduced. `"demo"` becomes a valid third value alongside `"paper"` and `"live"`.

---

## Credential Naming

Mirrors the existing `KALSHI_API_KEY` / `KALSHI_PRIVATE_KEY` pattern exactly:

| Purpose | Env var | Format |
|---------|---------|--------|
| Live API key ID | `KALSHI_API_KEY` | string (unchanged) |
| Live private key | `KALSHI_PRIVATE_KEY` | PEM string or path (unchanged) |
| Demo API key ID | `KALSHI_DEMO_API_KEY` | string (new, empty placeholder) |
| Demo private key | `KALSHI_DEMO_PRIVATE_KEY` | PEM string or path (new, empty placeholder) |

---

## Files Changed

| File | Change |
|------|--------|
| `bot.py` | 5 targeted edits (see below) |
| `config.json` | No edit — `"mode": "demo"` is already a valid string value |
| `.env` / Railway env vars | User adds `KALSHI_DEMO_API_KEY` + `KALSHI_DEMO_PRIVATE_KEY` after scaffolding |

---

## The 5 Edits to bot.py

### Edit 1 — Constants block (~line 61)

Replace the single hardcoded `KALSHI_BASE_URL` with two named constants plus a mutable that gets set at startup:

```python
# Before
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# After
KALSHI_LIVE_BASE_URL  = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE_URL  = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_BASE_URL       = KALSHI_LIVE_BASE_URL  # overwritten at startup based on mode
```

### Edit 2 — `load_kalshi_credentials()` (~line 666)

Make credential loading mode-aware. Accept `mode` parameter. Load the right env vars based on mode. Set `KALSHI_BASE_URL` global. Hard-error if demo creds are missing when mode is `"demo"`.

```python
def load_kalshi_credentials(mode: str) -> None:
    global api_key, private_key, KALSHI_BASE_URL

    if mode == "paper":
        return  # no credentials needed

    if mode == "demo":
        KALSHI_BASE_URL = KALSHI_DEMO_BASE_URL
        key_id_var, pem_var = "KALSHI_DEMO_API_KEY", "KALSHI_DEMO_PRIVATE_KEY"
        env_label = "DEMO"
    else:  # live
        KALSHI_BASE_URL = KALSHI_LIVE_BASE_URL
        key_id_var, pem_var = "KALSHI_API_KEY", "KALSHI_PRIVATE_KEY"
        env_label = "LIVE"

    api_key = os.environ.get(key_id_var, "").strip()
    pem_val = os.environ.get(pem_var, "").strip()

    if not api_key:
        sys.exit(f"ERROR: {key_id_var} is not set (required for {env_label} mode).")
    if not pem_val:
        sys.exit(f"ERROR: {pem_var} is not set (required for {env_label} mode).")

    # ... existing PEM loading logic (path vs inline string) unchanged ...
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
```

### Edit 3 — Startup call site

Pass mode into the credential loader:

```python
# Before
load_kalshi_credentials()

# After
load_kalshi_credentials(mode=cfg["mode"])
```

### Edit 4 — Startup assertions + activation print (~line 4107)

After credentials are loaded, before accepting any orders:

```python
# Before
is_live = (mode == "live")

# After
is_live = mode in ("live", "demo")

if mode == "demo":
    assert KALSHI_BASE_URL == KALSHI_DEMO_BASE_URL, (
        f"SAFETY: demo mode must use demo URL; got {KALSHI_BASE_URL}"
    )
    assert api_key, "SAFETY: demo mode requires KALSHI_DEMO_API_KEY"
    assert private_key is not None, "SAFETY: demo mode requires KALSHI_DEMO_PRIVATE_KEY"
    masked = api_key[:6] + "..." if len(api_key) > 6 else "***"
    print(f"[DEMO MODE] Base URL : {KALSHI_BASE_URL}")
    print(f"[DEMO MODE] API key  : {masked}")

elif mode == "live":
    assert KALSHI_BASE_URL == KALSHI_LIVE_BASE_URL, (
        f"SAFETY: live mode must use live URL; got {KALSHI_BASE_URL}"
    )
    assert api_key, "SAFETY: live mode requires KALSHI_API_KEY"
    assert private_key is not None, "SAFETY: live mode requires KALSHI_PRIVATE_KEY"
    masked = api_key[:6] + "..." if len(api_key) > 6 else "***"
    print(f"[LIVE MODE] Base URL : {KALSHI_BASE_URL}")
    print(f"[LIVE MODE] API key  : {masked}")
```

Guard that prevents demo from falling back to live URL if env var is missing: handled in Edit 2 — `sys.exit()` fires before assertions are reached.

### Edit 5 — Daily loss limit (DLL) guard (~line 2734)

Current behavior: live DLL → flip mode to `"paper"`.  
New behavior: demo DLL → `bot_enabled = false` + Telegram notification (no paper flip).

```python
async def trigger_loss_limit(config, reason: str) -> None:
    mode = config.get("mode", "paper")

    if mode == "paper":
        return  # already safe

    if mode == "demo":
        cfg = load_config()
        cfg["bot_enabled"] = False
        save_config(cfg)
        msg = f"🛑 <b>DEMO DLL triggered</b>\n{reason}\nBot disabled. Re-enable manually."
        await send_telegram(msg)
        log.warning(f"Demo DLL triggered — bot_enabled set to False. Reason: {reason}")
        return

    # live: existing paper-flip behavior (unchanged)
    cfg = load_config()
    cfg["mode"] = "paper"
    save_config(cfg)
    log.warning(f"Live DLL triggered — mode flipped to paper. Reason: {reason}")
```

---

## What is Explicitly NOT Changed

- `place_order()` body — demo traffic flows through the existing live code path unchanged; mode string "demo" tags the DB write automatically
- All 18+ `KALSHI_BASE_URL` references in market-data fetch functions — they use the global set at startup
- `src/kalshi_botv3/` package — untouched
- Dashboard / UI / toggle button — untouched
- Live mode DLL behavior — unchanged (still flips to paper)
- Mode icon in trade log (`📄` / `💵`) — untouched (user said no UI changes)

---

## How to Test End-to-End

1. Add `KALSHI_DEMO_API_KEY` and `KALSHI_DEMO_PRIVATE_KEY` to your Railway/local env
2. Set `BOT_MODE=demo` (or edit `config.json` → `"mode": "demo"`)
3. Start the bot — confirm startup prints:
   ```
   [DEMO MODE] Base URL : https://demo-api.kalshi.co/trade-api/v2
   [DEMO MODE] API key  : abc123...
   ```
4. Confirm `bot_enabled: true` in config and let it run one cycle — check logs for a SKIP or SIGNAL event with `mode=demo`
5. Query `kalshi_bot.db` → `trades` table: confirm `mode = 'demo'` on any recorded activity
6. To test DLL guard: temporarily lower `daily_loss_limit_dollars` to $0.01 and trigger a fill; confirm `bot_enabled` flips to `false` and Telegram fires
7. To verify no live orders: check your live Kalshi account — zero activity should appear

---

## Safety Summary

| Risk | Mitigation |
|------|-----------|
| Demo creds accidentally used for live | Startup assertion: `KALSHI_BASE_URL == KALSHI_LIVE_BASE_URL` when mode=="live" |
| Live creds accidentally used for demo | Startup assertion: `KALSHI_BASE_URL == KALSHI_DEMO_BASE_URL` when mode=="demo" |
| Demo creds missing → silent live fallback | `sys.exit()` in credential loader before any assertion is reached |
| Wrong URL set | Two named constants prevent typos; assertions cross-check mode↔URL |
| DB records mixed up | `mode` column records "demo" vs "live" string; queryable |
