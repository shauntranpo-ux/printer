#!/bin/bash
# Start bot worker in background, web server in foreground
python runner.py &
exec python -m gunicorn server:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --timeout 120
