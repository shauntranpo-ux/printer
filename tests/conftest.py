import logging
import pytest
from unittest.mock import patch


def pytest_configure(config):
    logging.basicConfig(level=logging.WARNING)


@pytest.fixture(autouse=True)
def disable_quiet_hours(request):
    """Patch _is_quiet_hours to return False for all tests except quiet-hours unit tests.

    Tests named test_quiet_hours_* test the gate logic directly and bypass this patch.
    All other strategy tests run as if quiet hours is disabled so time-of-day doesn't
    cause gates to be unreachable.
    """
    if "test_quiet_hours" in (request.node.name or ""):
        yield
        return
    with patch("bot_strategy._is_quiet_hours", return_value=False):
        yield

