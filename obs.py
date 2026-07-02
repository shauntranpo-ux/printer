"""
obs.py - Observability helpers: structured JSON logging + error ring buffer.

Usage:
    import obs
    obs.setup_logging("bot")   # call once at process startup

    # In server.py to retrieve last captured ERROR:
    err = obs.get_last_error()  # -> {"ts": ..., "msg": ..., "logger": ...} or None

Environment:
    LOG_FORMAT=text   human-readable (dev); default is json (Railway)
"""

import json
import logging
import logging.handlers
import os
from collections import deque
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%S.") +
                  f"{int(record.msecs):03d}Z",
            "level":   record.levelname,
            "service": self._service,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        _skip = {
            "args", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "message",
            "module", "msecs", "msg", "name", "pathname", "process",
            "processName", "relativeCreated", "stack_info", "thread",
            "threadName",
        }
        for key, val in record.__dict__.items():
            if key not in _skip and not key.startswith("_"):
                try:
                    json.dumps(val)
                    obj[key] = val
                except (TypeError, ValueError):
                    obj[key] = repr(val)
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, separators=(",", ":"))


class _ErrorRingBuffer(logging.Handler):
    """In-memory ring buffer of the last N ERROR-level log records."""

    def __init__(self, maxsize: int = 10) -> None:
        super().__init__(level=logging.ERROR)
        self._buf: deque[dict] = deque(maxlen=maxsize)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append({
                "ts":     datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "msg":    record.getMessage(),
                "logger": record.name,
            })
        except Exception:
            pass

    def last(self) -> "dict | None":
        return self._buf[-1] if self._buf else None


_ring: "_ErrorRingBuffer | None" = None


def setup_logging(service: str) -> None:
    """Install JSON (or text) formatter on the root logger + error ring buffer."""
    global _ring

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT", "json") == "text":
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
    else:
        handler.setFormatter(_JsonFormatter(service))
    root.addHandler(handler)

    _ring = _ErrorRingBuffer(maxsize=10)
    root.addHandler(_ring)


def get_last_error() -> "dict | None":
    """Return the most recent ERROR record captured since setup_logging, or None."""
    return _ring.last() if _ring is not None else None
