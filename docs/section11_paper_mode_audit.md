# Section 11 — Paper Mode Safety Audit

Audited: 2026-04-18. Branch: `refactor/base-strategy`.

## Order-submission call sites

### `place_order` (bot.py:2595)
**Gate**: `if mode == "paper": return early` at **line 2619**.
In paper mode returns `{"fill_confirmed": True, "order_id": "paper_<ts>"}` immediately
without touching the Kalshi `/portfolio/orders` POST endpoint.
The live HTTP POST (line 2670) is unreachable in paper mode.

### `sell_position` (bot.py:2795)
**Gate**: `if mode == "paper": return current_bid` at **line 2810**.
In paper mode returns the simulated bid price immediately without
touching the Kalshi `/portfolio/orders` POST endpoint.
The live HTTP POST (line 2830) is unreachable in paper mode.

### Call site: `run_window` → `place_order` (bot.py:3428)
`mode` is read from `config["mode"]` (set to `"paper"` in `config.json`).
Passed directly into `place_order`. No bypass path exists.

## Read-only Kalshi GET calls (not order submissions)

| Line | Endpoint | Purpose |
|------|----------|---------|
| 797 | `GET /markets` | Market discovery |
| 923 | `GET /markets` | Per-asset market fetch |
| 1068 | `GET /markets/{ticker}/orderbook` | Orderbook fetch |
| 1100 | `GET /markets/{ticker}` | Market detail |
| 2766 | `GET /portfolio/positions` | Post-order portfolio check (inside live branch only) |
| 3589 | `GET /markets/{ticker}` | Settlement result fetch |
| 4130 | `GET /portfolio/balance` | Balance check (preflight) |
| 4158–4209 | `GET /markets`, `/series` | Market discovery debug |

All are GETs. None submit orders. All run regardless of mode (safe).

## Verdict

Paper mode is structurally safe. No order submission path can execute
without `config["mode"] == "live"`. The gates are at the function entry
of both `place_order` and `sell_position`, making accidental live orders
impossible while `mode = "paper"` is set in `config.json`.
