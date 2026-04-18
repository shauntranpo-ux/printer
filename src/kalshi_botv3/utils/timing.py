import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import structlog

FuncT = TypeVar("FuncT", bound=Callable[..., Any])


def log_duration(
    logger: structlog.BoundLogger, event_name: str
) -> Callable[[FuncT], FuncT]:
    """Decorator that logs execution time in milliseconds.

    Works for both sync and async functions. On exception, logs at ERROR level
    with exc_info and re-raises.
    """

    def decorator(func: FuncT) -> FuncT:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.monotonic()
                try:
                    result = await cast(Awaitable[Any], func(*args, **kwargs))
                    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                    logger.info(event_name, duration_ms=elapsed_ms)
                    return result
                except Exception:
                    elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                    logger.error(event_name, duration_ms=elapsed_ms, exc_info=True)
                    raise

            return cast(FuncT, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                logger.info(event_name, duration_ms=elapsed_ms)
                return result
            except Exception:
                elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                logger.error(event_name, duration_ms=elapsed_ms, exc_info=True)
                raise

        return cast(FuncT, sync_wrapper)

    return decorator
