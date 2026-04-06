#!/bin/bash
# Restart runner.py automatically if it ever dies
while true; do
    python runner.py
    echo "[start.sh] runner.py exited — restarting in 5s..."
    sleep 5
done &
exec python -m gunicorn server:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --timeout 120
