# Money Printer - Kalshi Trading Bot

Automated trading bot for Kalshi's 15-minute crypto up/down prediction markets. Trades BTC, ETH, SOL, XRP, and DOGE with two fair-value strategies: S1 prices the alts off BTC's lead (cross-asset dislocation) and S2 prices each contract off the current spot (Bachelier fair value vs the market mid). Deployed on Railway.

**Status: actively deployed. No rewrite in progress.**

Source: https://github.com/shauntranpo-ux/printer

---

## Source of Truth

**Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before touching any code.** It lists every production file, every env var, and the status of the abandoned rewrite in `src/`.

---

## Quickstart (live bot, local)

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Copy and fill in env vars
cp .env.example .env
# edit .env - at minimum: KALSHI_API_KEY, KALSHI_PRIVATE_KEY

# 3. Run
bash start.sh
```

`start.sh` launches `runner.py` (bot worker manager) and `gunicorn server:app` (dashboard). Dashboard defaults to `http://localhost:5000`.

### Required env vars

| Variable | Purpose |
|---|---|
| `KALSHI_API_KEY` | Live API key (UUID) |
| `KALSHI_PRIVATE_KEY` | RSA private key PEM string or path to `.pem` file |
| `TELEGRAM_BOT_TOKEN` | Optional - alerts and trade notifications |
| `TELEGRAM_CHAT_ID` | Required if Telegram alerts enabled |
| `BOT_DB_FILE` | SQLite DB path; default `kalshi_bot.db` |

See `.env.example` for the full variable list, or [`ARCHITECTURE.md § Live Config`](ARCHITECTURE.md#live-config) for grep-verified line references.

---

## Running Tests

```bash
.venv/Scripts/pytest -q         # Windows (venv)
pytest -q                       # Linux / Railway
```

---

## Asset Support

5 assets: BTC, ETH, SOL, XRP, DOGE. Default enabled: ETH, SOL, XRP. See [`ARCHITECTURE.md § Asset Support`](ARCHITECTURE.md#asset-support).
