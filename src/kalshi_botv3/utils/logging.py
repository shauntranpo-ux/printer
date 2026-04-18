import logging as _stdlib_logging
import sys
from typing import Any, cast

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog once at startup.

    Writes JSON to stderr when not a TTY; ConsoleRenderer otherwise.
    Call this exactly once before any log statements.
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if sys.stderr.isatty():
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    level_int = _stdlib_logging.getLevelName(level.upper())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )


def bind_context(**kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def get_logger(name: str) -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger(name))


def get_trade_logger() -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger("trades").bind(channel="trades"))
