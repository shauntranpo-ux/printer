# Railway Setup Notes
- Volume must be mounted at /app to persist kalshi_bot.db
- Set env vars: KALSHI_API_KEY, KALSHI_PRIVATE_KEY, ANTHROPIC_API_KEY
- The KALSHI_PRIVATE_KEY must have literal \n between lines (not real line breaks)
- Web dashboard runs on the Railway-assigned PORT automatically
- bot.py runs as the worker process
- SOLANA_RPC_URL: Optional. Solana RPC endpoint used by the SOL strategy network
  health kill switch. Defaults to https://api.mainnet-beta.solana.com (public).
  For production, set to a private endpoint (Helius, QuickNode) for better
  reliability. If this URL is unreachable or returns non-"ok", all SOL windows
  are skipped (fail-safe behavior).
