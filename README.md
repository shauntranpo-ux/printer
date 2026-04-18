# kalshi-bot

A private automated trading bot for Kalshi's 15-minute crypto up/down markets. It monitors
price action across 7 assets (BTC, ETH, XRP, SOL, DOGE, HYPE, BNB), evaluates edge via
configurable strategies, and places binary yes/no positions through Kalshi's REST and
WebSocket APIs. Built on Python 3.11+ with async I/O throughout, deployed to Railway.

## Quickstart

```bash
uv sync
uv run pytest
```

## Folder Structure

```
kalshi-bot/
├── src/
│   └── kalshi_bot/
│       ├── config/       # Settings and environment loading
│       ├── db/           # SQLAlchemy models and migrations (Alembic)
│       ├── exchange/     # Price feed connectors (Binance, etc.)
│       ├── kalshi/       # Kalshi REST + WebSocket client
│       ├── features/     # Feature engineering for strategies
│       ├── strategies/   # Signal generation and sizing
│       ├── scheduler/    # APScheduler job management
│       ├── executor/     # Order execution and position tracking
│       └── utils/        # Shared helpers
├── tests/                # pytest test suite
├── scripts/              # One-off utility scripts
└── .github/
    └── workflows/        # CI workflows
```

## Status

**Section 1 complete — scaffolding only.**
No business logic, strategies, or data connectors have been implemented yet.
