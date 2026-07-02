"""
notify.py - Centralized alert dispatch: Telegram + volume-backed alerts.log.

Usage:
    import notify
    notify.send_alert("WARNING", "Bot crash loop detected - halting restarts.")

Telegram sends are fire-and-forget. If Telegram fails or creds are missing,
the alert is still written to alerts.log on the Railway volume so nothing is
silently lost.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("notify")


def send_alert(level: str, text: str) -> None:
    """Send alert to Telegram (if configured) and append to alerts.log on volume."""
    _send_telegram(text)
    _append_log(level, text)


def _send_telegram(text: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "")
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        log.warning(f"Telegram alert error: {exc}")


def _alerts_log_path() -> str:
    db = os.environ.get("BOT_DB_FILE", "")
    if db:
        return os.path.join(os.path.dirname(os.path.abspath(db)), "alerts.log")
    return "alerts.log"


def _append_log(level: str, text: str) -> None:
    entry = json.dumps({
        "ts":    datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg":   text,
    }, separators=(",", ":"))
    try:
        with open(_alerts_log_path(), "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except Exception as exc:
        log.warning(f"alerts.log write error: {exc}")
