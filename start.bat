@echo off
set KALSHI_API_KEY=5789bc67-3c5d-4d16-b6f0-3eaa6feb1f14
set KALSHI_PRIVATE_KEY=%~dp0kalshi_private_key.pem
set ANTHROPIC_API_KEY=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
cd /d %~dp0
python runner.py
pause
