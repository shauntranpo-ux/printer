import io
import json
from collections.abc import Generator
from typing import Any

import pytest
import structlog

from kalshi_botv3.utils.logging import bind_context, configure_logging, get_logger
from kalshi_botv3.utils.timing import log_duration


@pytest.fixture(autouse=True)
def _clear_contextvars() -> Generator[None, None, None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def json_buf() -> Generator[io.StringIO, None, None]:
    """Configure structlog with JSON → StringIO for the duration of one test."""
    buf = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(buf),
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        cache_logger_on_first_use=False,
    )
    yield buf
    configure_logging("INFO")


def _parse_last_line(buf: io.StringIO) -> dict[str, Any]:
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)  # type: ignore[no-any-return]


def test_logger_emits_json(json_buf: io.StringIO) -> None:
    get_logger("test").info("test_event", market="BTC")
    data = _parse_last_line(json_buf)
    assert data["event"] == "test_event"
    assert data["level"] == "info"
    assert "timestamp" in data
    assert data["market"] == "BTC"


def test_bind_context_propagates(json_buf: io.StringIO) -> None:
    bind_context(request_id="abc-123", asset="ETH")
    get_logger("test").info("ctx_event")
    data = _parse_last_line(json_buf)
    assert data["request_id"] == "abc-123"
    assert data["asset"] == "ETH"


def test_log_duration_sync() -> None:
    with structlog.testing.capture_logs() as logs:

        @log_duration(get_logger("test"), "sync_op")
        def add(x: int, y: int) -> int:
            return x + y

        result = add(2, 3)

    assert result == 5
    assert len(logs) == 1
    assert logs[0]["event"] == "sync_op"
    assert isinstance(logs[0]["duration_ms"], float)
    assert logs[0]["duration_ms"] >= 0


async def test_log_duration_async() -> None:
    with structlog.testing.capture_logs() as logs:

        @log_duration(get_logger("test"), "async_op")
        async def fetch(val: int) -> int:
            return val * 2

        result = await fetch(7)

    assert result == 14
    assert len(logs) == 1
    assert logs[0]["event"] == "async_op"
    assert isinstance(logs[0]["duration_ms"], float)


def test_log_duration_logs_exception() -> None:
    with structlog.testing.capture_logs() as logs:

        @log_duration(get_logger("test"), "failing_op")
        def boom() -> None:
            raise ValueError("intentional")

        with pytest.raises(ValueError, match="intentional"):
            boom()

    assert len(logs) == 1
    assert logs[0]["event"] == "failing_op"
    assert "exc_info" in logs[0]
    assert "duration_ms" in logs[0]
