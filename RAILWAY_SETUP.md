# Railway Setup Notes

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the authoritative layout. This file covers Railway-specific deployment details only.

---

## Service: Web (dashboard + bot worker)

**Start command:** `bash start.sh` (defined in `Procfile`)

`start.sh` does two things in a single Railway service:
1. Spawns `python runner.py` in a background restart loop (bot worker manager)
2. Execs `gunicorn server:app --bind 0.0.0.0:${PORT:-5000}` (dashboard web server)

> ~~"bot.py runs as the worker process"~~ - **this was wrong.** `runner.py` is the worker manager. It spawns `bot.py` as a subprocess per enabled strategy. See `start.sh` and `runner.py`.

---

## Volume

Mount a Railway volume at `/app/data` (not `/app`). Set:

```
BOT_DB_FILE=/app/data/kalshi_bot.db
BOT_STATE_FILE=/app/data/bot_state.json
```

The bot writes `brain.log` and `alerts.log` to the same directory as `BOT_DB_FILE`.

---

## Required Environment Variables

Minimum for live mode:

| Variable | Value |
|---|---|
| `KALSHI_API_KEY` | Live API key UUID |
| `KALSHI_PRIVATE_KEY` | RSA private key - literal `\n` between lines (not real newlines) |
| `BOT_DB_FILE` | `/app/data/kalshi_bot.db` |
| `BOT_STATE_FILE` | `/app/data/bot_state.json` |
| `BOT_MODE` | `live` |
| `BOT_ENABLED` | `true` |

Full variable list: [`.env.example`](.env.example) and [`ARCHITECTURE.md § Live Config`](ARCHITECTURE.md#live-config).

---

## Healthcheck

Railway can poll `GET /healthz`. Returns `200 {"status":"ok"}` when `bot_state.json` was updated within the last 120 seconds; `503 {"status":"stale","age":N}` otherwise.

---

## SOLANA_RPC_URL - Status Unknown

The previous version of this file documented `SOLANA_RPC_URL` as an optional env var for a SOL network health kill switch. **This variable is not read by any `.py` file in the current codebase** (`grep -rn SOLANA_RPC *.py` returns no results). It may have been removed from the code or was always doc-only. Do not set it - it has no effect.

---

## TODO - Owner to Fill In

The following is unknown and should be documented by the owner:

```
[ ] Worker service override command: If "zesty-respect" (or any other Railway
    service) runs a separate worker with a different start command, document it here.
    The Procfile only shows: web: bash start.sh

[ ] Env var diff between web and worker services: If the worker service sets
    different BOT_DB_FILE / BOT_STATE_FILE / BOT_MODE values than the web service,
    list them here to avoid cross-service confusion.

[ ] Volume mount on worker service: Confirm whether the worker service shares
    the same /app/data volume or has a separate mount.
```
