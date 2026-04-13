@echo off
set KALSHI_API_KEY=5789bc67-3c5d-4d16-b6f0-3eaa6feb1f14
set KALSHI_PRIVATE_KEY=%~dp0kalshi_private_key.pem
set ANTHROPIC_API_KEY=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
cd /d %~dp0
python runner.py
pause
