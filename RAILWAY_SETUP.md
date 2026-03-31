# Railway Setup Notes
- Volume must be mounted at /app to persist kalshi_bot.db
- Set env vars: KALSHI_API_KEY, KALSHI_PRIVATE_KEY, ANTHROPIC_API_KEY
- The KALSHI_PRIVATE_KEY must have literal \n between lines (not real line breaks)
- Web dashboard runs on the Railway-assigned PORT automatically
- bot.py runs as the worker process
