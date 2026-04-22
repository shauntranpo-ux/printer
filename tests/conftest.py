import sys
import os
# Allow `from strategy_a.features.har_rv import ...` in all test files
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

import pytest

from kalshi_botv3.utils.logging import configure_logging


@pytest.fixture(scope="session", autouse=True)
def setup_logging() -> None:
    configure_logging("INFO")
