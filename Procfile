web: python -m gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --preload --capture-output --log-level debug
worker: python runner.py
